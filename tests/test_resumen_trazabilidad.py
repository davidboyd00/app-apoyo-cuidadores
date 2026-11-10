"""Nota de trazabilidad del resumen médico (validez probatoria).

La nota la arma el código, no el LLM: aunque el modelo omita algo, el médico
siempre ve cuántas entradas se consideraron, cuáles se corrigieron (cuándo y
por quién) y cuántas se anularon y quedaron fuera.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db, require_consent
from app.main import app
from app.routers import summaries

PACIENTE = "00000000-0000-0000-0000-0000000000aa"
AUTORA = "00000000-0000-0000-0000-000000000001"
E1 = "00000000-0000-0000-0000-0000000000e1"
E2 = "00000000-0000-0000-0000-0000000000e2"
URL = f"/patients/{PACIENTE}/summaries"

FILA_RESUMEN = {
    "id": "00000000-0000-0000-0000-0000000000ff",
    "patient_id": PACIENTE,
    "desde": "2026-10-01T00:00:00+00:00",
    "hasta": "2026-10-31T00:00:00+00:00",
    "contenido": "x",
    "created_at": "2026-10-31T00:00:00+00:00",
}


def _ej(data):
    return SimpleNamespace(data=data)


def _tabla(*resultados):
    t = MagicMock()
    for m in ("select", "eq", "gte", "lte", "order", "in_", "insert", "limit"):
        getattr(t, m).return_value = t
    t.execute.side_effect = list(resultados)
    return t


@pytest.fixture
def ctx(monkeypatch):
    tablas: dict[str, MagicMock] = {}
    db = MagicMock()
    db.table.side_effect = lambda nombre: tablas[nombre]
    vistos: dict[str, str] = {}

    async def llm_falso(contexto: str, pregunta: str) -> str:
        vistos["contexto"] = contexto
        return "Resumen de la IA."

    monkeypatch.setattr(summaries, "ask_llm", llm_falso)
    app.dependency_overrides[get_current_user] = lambda: {"sub": AUTORA}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_consent] = lambda: None
    try:
        yield SimpleNamespace(client=TestClient(app), tablas=tablas, vistos=vistos)
    finally:
        app.dependency_overrides.clear()


def _contenido_guardado(ctx) -> str:
    return ctx.tablas["summaries"].insert.call_args.args[0]["contenido"]


def test_sin_correcciones_lo_declara_explicitamente(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(
        _ej(
            [
                {
                    "id": E1,
                    "occurred_at": "2026-10-12T17:30:00+00:00",
                    "kind": "medicamento",
                    "content": "Insulina 8 UI",
                    "editada": False,
                }
            ]
        ),
        _ej([]),  # anuladas del período
    )
    ctx.tablas["summaries"] = _tabla(_ej([FILA_RESUMEN]))

    r = ctx.client.post(URL)

    assert r.status_code == 201
    contenido = _contenido_guardado(ctx)
    assert contenido.startswith("Resumen de la IA.")
    assert "generada por el sistema, no por IA" in contenido
    assert (
        "Entradas consideradas: 1 · Corregidas: 0 · Anuladas y excluidas del resumen: 0"
        in contenido
    )
    assert "No hubo correcciones ni anulaciones en el período." in contenido


def test_lista_correcciones_con_autor_fecha_y_conteo_y_cuenta_anuladas(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(
        _ej(
            [
                {
                    "id": E1,
                    "occurred_at": "2026-10-12T17:30:00+00:00",
                    "kind": "medicamento",
                    "content": "Insulina 6 UI",
                    "editada": True,
                },
                {
                    "id": E2,
                    "occurred_at": "2026-10-11T12:00:00+00:00",
                    "kind": "nota",
                    "content": "Durmió bien",
                    "editada": False,
                },
            ]
        ),
        _ej([{"id": "anulada-1"}, {"id": "anulada-2"}]),
    )
    ctx.tablas["log_entry_revisions"] = _tabla(
        _ej(
            [  # orden: más reciente primero
                {"entry_id": E1, "author_id": AUTORA, "created_at": "2026-10-12T19:05:00+00:00"},
                {"entry_id": E1, "author_id": AUTORA, "created_at": "2026-10-12T18:00:00+00:00"},
            ]
        )
    )
    ctx.tablas["profiles"] = _tabla(_ej([{"id": AUTORA, "nombre": "Ana"}]))
    ctx.tablas["summaries"] = _tabla(_ej([FILA_RESUMEN]))

    r = ctx.client.post(URL)

    assert r.status_code == 201
    contenido = _contenido_guardado(ctx)
    assert (
        "Entradas consideradas: 2 · Corregidas: 1 · Anuladas y excluidas del resumen: 2"
        in contenido
    )
    # Hora de Chile (UTC-3): 17:30 UTC -> 14:30, última corrección 19:05 UTC -> 16:05.
    assert (
        "- Entrada del 12-10-2026 14:30 (medicamento): corregida el 12-10-2026 16:05 "
        "por Ana (2 correcciones)." in contenido
    )
    # El LLM sabe que la entrada fue corregida y recibe la versión vigente.
    assert "medicamento [corregida]: Insulina 6 UI" in ctx.vistos["contexto"]


def test_nota_no_expone_contenido_clinico_ni_motivos(ctx):
    ctx.tablas["log_entries_vigentes"] = _tabla(
        _ej(
            [
                {
                    "id": E1,
                    "occurred_at": "2026-10-12T17:30:00+00:00",
                    "kind": "medicamento",
                    "content": "Anticoagulante 5 mg",
                    "editada": True,
                }
            ]
        ),
        _ej([]),
    )
    ctx.tablas["log_entry_revisions"] = _tabla(
        _ej([{"entry_id": E1, "author_id": AUTORA, "created_at": "2026-10-12T19:05:00+00:00"}])
    )
    ctx.tablas["profiles"] = _tabla(_ej([]))  # sin nombre registrado
    ctx.tablas["summaries"] = _tabla(_ej([FILA_RESUMEN]))

    ctx.client.post(URL)

    nota = _contenido_guardado(ctx).split("---", 1)[1]
    assert "Anticoagulante" not in nota
    assert "por un miembro del grupo." in nota
    # Solo se pidieron columnas sin motivo ni contenido.
    ctx.tablas["log_entry_revisions"].select.assert_called_with("entry_id, author_id, created_at")
