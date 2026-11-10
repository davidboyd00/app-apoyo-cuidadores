"""Historial inmutable y carga offline a nivel de API.

Cubre el comportamiento de los endpoints con la base simulada (MagicMock), en
la misma línea que test_entries_validacion.py. Las garantías de la base (RLS,
insert-only, unicidad de client_id) están en tests/test_rls.py.

- Revisar y anular: reglas de negocio (no se reabre una entrada anulada, 404
  cuando no existe o no hay acceso).
- GET: por defecto oculta las anuladas.
- Offline: POST con client_id es idempotente (reintento = 200, misma fila) y
  registrado_en no puede venir del futuro.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app.deps import get_current_user, get_db, require_consent
from app.main import app

PACIENTE = "00000000-0000-0000-0000-0000000000aa"
ENTRADA = "00000000-0000-0000-0000-0000000000bb"
AUTOR = "00000000-0000-0000-0000-000000000001"
URL = f"/patients/{PACIENTE}/entries"
CLIENT_ID = "33333333-3333-3333-3333-333333333333"

FILA = {
    "id": ENTRADA,
    "patient_id": PACIENTE,
    "author_id": AUTOR,
    "kind": "medicamento",
    "content": "Enalapril 10 mg",
    "occurred_at": "2026-10-19T18:30:00+00:00",
    "source": "text",
    "created_at": "2026-10-19T18:31:00+00:00",
}
VIGENTE = FILA | {"anulada": False, "editada": False}
ANULADA = FILA | {"anulada": True, "editada": False}
CUERPO_EDITAR = {
    "motivo": "dosis mal anotada",
    "kind": "medicamento",
    "content": "Enalapril 5 mg",
    "occurred_at": "2026-10-19T18:30:00-03:00",
}


def _ej(data):
    return SimpleNamespace(data=data)


def _tabla(*resultados):
    """Tabla simulada: cada execute() devuelve el siguiente resultado."""
    t = MagicMock()
    for metodo in ("select", "eq", "limit", "order", "gte", "lt", "insert"):
        getattr(t, metodo).return_value = t
    t.execute.side_effect = list(resultados)
    return t


@pytest.fixture
def ctx():
    """Factory: fija las tablas simuladas y devuelve (client, db)."""
    tablas: dict[str, MagicMock] = {}
    db = MagicMock()
    db.table.side_effect = lambda nombre: tablas[nombre]
    db.rpc.return_value = MagicMock()  # auditoría

    app.dependency_overrides[get_current_user] = lambda: {"sub": AUTOR}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_consent] = lambda: None
    try:
        yield SimpleNamespace(client=TestClient(app), db=db, tablas=tablas)
    finally:
        app.dependency_overrides.clear()


# --- Revisar -----------------------------------------------------------------


def test_revisar_entrada_vigente_devuelve_estado_editado(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(
        _ej([VIGENTE]),  # pre-chequeo: existe y no está anulada
        _ej([VIGENTE | {"editada": True, "content": "Enalapril 5 mg"}]),  # estado final
    )
    ctx.tablas["log_entry_revisions"] = _tabla(_ej([{"id": "r1"}]))  # insert

    r = ctx.client.post(f"{URL}/{ENTRADA}/revisiones", json=CUERPO_EDITAR)

    assert r.status_code == 201
    assert r.json()["content"] == "Enalapril 5 mg"
    assert r.json()["editada"] is True
    insertada = ctx.tablas["log_entry_revisions"].insert.call_args.args[0]
    assert insertada["accion"] == "editar"
    assert insertada["motivo"] == "dosis mal anotada"
    assert "author_id" not in insertada  # lo pone la base (auth.uid()), no el cliente


def test_revisar_entrada_inexistente_es_404(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([]))
    r = ctx.client.post(f"{URL}/{ENTRADA}/revisiones", json=CUERPO_EDITAR)
    assert r.status_code == 404


def test_revisar_entrada_anulada_es_400(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([ANULADA]))
    r = ctx.client.post(f"{URL}/{ENTRADA}/revisiones", json=CUERPO_EDITAR)
    assert r.status_code == 400
    assert "anulada" in r.json()["detail"]
    ctx.tablas.setdefault("log_entry_revisions", _tabla())
    ctx.tablas["log_entry_revisions"].insert.assert_not_called()


# --- Anular ------------------------------------------------------------------


def test_anular_entrada_vigente(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([VIGENTE]), _ej([ANULADA]))
    ctx.tablas["log_entry_revisions"] = _tabla(_ej([{"id": "r1"}]))

    r = ctx.client.post(f"{URL}/{ENTRADA}/anulacion", json={"motivo": "paciente equivocado"})

    assert r.status_code == 201
    assert r.json()["anulada"] is True
    insertada = ctx.tablas["log_entry_revisions"].insert.call_args.args[0]
    assert insertada["accion"] == "anular"


def test_anular_entrada_ya_anulada_es_400(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([ANULADA]))
    ctx.tablas["log_entry_revisions"] = _tabla()
    r = ctx.client.post(f"{URL}/{ENTRADA}/anulacion", json={"motivo": "otra vez"})
    assert r.status_code == 400
    ctx.tablas["log_entry_revisions"].insert.assert_not_called()


def test_anular_entrada_inexistente_es_404(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([]))
    r = ctx.client.post(f"{URL}/{ENTRADA}/anulacion", json={"motivo": "no existe"})
    assert r.status_code == 404


# --- Historial y lectura -----------------------------------------------------


def test_listar_revisiones_devuelve_historial_en_orden(ctx):
    revisiones = [
        {
            "id": str(uuid4()),
            "entry_id": ENTRADA,
            "patient_id": PACIENTE,
            "author_id": AUTOR,
            "accion": "editar",
            "motivo": "dosis mal anotada",
            "kind": "medicamento",
            "content": "Enalapril 5 mg",
            "occurred_at": "2026-10-19T21:30:00+00:00",
            "created_at": "2026-10-19T22:00:00+00:00",
        }
    ]
    ctx.tablas["log_entry_revisions"] = _tabla(_ej(revisiones))

    r = ctx.client.get(f"{URL}/{ENTRADA}/revisiones")

    assert r.status_code == 200
    assert r.json()[0]["accion"] == "editar"
    ctx.tablas["log_entry_revisions"].order.assert_called_with("created_at", desc=False)


def test_listar_oculta_anuladas_por_defecto(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([VIGENTE]))
    ctx.client.get(URL)
    ctx.tablas["log_entries_vigentes"].eq.assert_any_call("anulada", False)


def test_listar_incluir_anuladas_quita_el_filtro(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([ANULADA]))
    r = ctx.client.get(f"{URL}?incluir_anuladas=true")
    assert r.status_code == 200
    llamadas = ctx.tablas["log_entries_vigentes"].eq.call_args_list
    assert ("anulada", False) not in [c.args for c in llamadas]


# --- Offline: idempotencia ---------------------------------------------------


def _cuerpo_offline(**extra):
    base = {
        "kind": "medicamento",
        "content": "Insulina 8 UI",
        "occurred_at": "2026-10-19T15:30:00-03:00",
        "client_id": CLIENT_ID,
        "registrado_en": "2026-10-19T15:31:00-03:00",
    }
    return base | extra


def test_crear_offline_primera_vez_es_201(ctx):
    ctx.tablas["log_entries"] = _tabla(_ej([]), _ej([FILA]))  # no existe; insert
    r = ctx.client.post(URL, json=_cuerpo_offline())
    assert r.status_code == 201
    assert r.json()["anulada"] is False
    insertada = ctx.tablas["log_entries"].insert.call_args.args[0]
    assert insertada["client_id"] == CLIENT_ID
    assert insertada["registrado_en"].startswith("2026-10-19T15:31:00")


def test_reintento_con_mismo_client_id_devuelve_la_existente(ctx):
    ctx.tablas["log_entries"] = _tabla(_ej([FILA]))  # ya existe
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([VIGENTE]))

    r = ctx.client.post(URL, json=_cuerpo_offline())

    assert r.status_code == 200
    assert r.json()["id"] == ENTRADA
    ctx.tablas["log_entries"].insert.assert_not_called()  # no duplica


def test_carrera_entre_dos_reintentos_se_resuelve_sin_duplicar(ctx):
    """Dos reintentos casi simultáneos: el segundo insert choca con 23505 y
    el servidor devuelve la fila que ganó la carrera."""
    error = APIError({"code": "23505", "message": "duplicate", "details": "", "hint": ""})
    tabla = _tabla(_ej([]), error, _ej([FILA]))  # select vacío, insert choca, re-select
    ctx.tablas["log_entries"] = tabla
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([VIGENTE]))

    r = ctx.client.post(URL, json=_cuerpo_offline())

    assert r.status_code == 200
    assert r.json()["id"] == ENTRADA


def test_registrado_en_futuro_es_422(ctx):
    r = ctx.client.post(URL, json=_cuerpo_offline(registrado_en="2099-01-01T00:00:00-03:00"))
    assert r.status_code == 422
    campos = {e["campo"] for e in r.json()["errores"]}
    assert "registrado_en" in campos


def test_registrado_en_sin_zona_horaria_es_422(ctx):
    r = ctx.client.post(URL, json=_cuerpo_offline(registrado_en="2026-10-19T15:31:00"))
    assert r.status_code == 422


def test_dosis_offline_reintento_devuelve_200(ctx):
    med = "00000000-0000-0000-0000-0000000000cc"
    dosis = {
        "id": str(uuid4()),
        "patient_id": PACIENTE,
        "medication_id": med,
        "author_id": AUTOR,
        "scheduled_for": "2026-10-19T12:00:00+00:00",
        "taken_at": "2026-10-19T12:05:00+00:00",
        "status": "tomada",
        "notas": None,
        "created_at": "2026-10-19T15:40:00+00:00",
        "registrado_en": "2026-10-19T12:05:00+00:00",
    }
    ctx.tablas["medication_doses"] = _tabla(_ej([dosis]))  # ya existe

    r = ctx.client.post(
        f"/patients/{PACIENTE}/medications/{med}/doses",
        json={
            "scheduled_for": "2026-10-19T12:00:00+00:00",
            "taken_at": "2026-10-19T12:05:00+00:00",
            "status": "tomada",
            "client_id": CLIENT_ID,
            "registrado_en": "2026-10-19T12:05:00+00:00",
        },
    )

    assert r.status_code == 200
    ctx.tablas["medication_doses"].insert.assert_not_called()
