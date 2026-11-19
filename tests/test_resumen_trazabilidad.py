"""Resúmenes médicos: generación en segundo plano, reutilización y trazabilidad.

La nota de trazabilidad la arma el código, no el LLM: aunque el modelo omita
algo, el médico ve cuántas entradas se consideraron, cuáles se corrigieron
(cuándo y por quién) y cuántas se anularon y quedaron fuera.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import resumen
from app.deps import get_current_user, get_db, require_consent
from app.main import app

PACIENTE = "00000000-0000-0000-0000-0000000000aa"
AUTORA = "00000000-0000-0000-0000-000000000001"
E1 = "00000000-0000-0000-0000-0000000000e1"
E2 = "00000000-0000-0000-0000-0000000000e2"
RESUMEN = "00000000-0000-0000-0000-0000000000ff"
URL = f"/patients/{PACIENTE}/summaries"


def _ahora() -> datetime:
    return datetime.now(UTC)


def _fila(estado="generando", dias=30, creado_hace=timedelta(0), contenido=None):
    hasta = _ahora() - creado_hace
    return {
        "id": RESUMEN,
        "patient_id": PACIENTE,
        "desde": (hasta - timedelta(days=dias)).isoformat(),
        "hasta": hasta.isoformat(),
        "contenido": contenido,
        "estado": estado,
        "created_at": hasta.isoformat(),
    }


def _ej(data):
    return SimpleNamespace(data=data)


def _tabla(*resultados):
    t = MagicMock()
    for m in ("select", "eq", "gte", "lte", "order", "in_", "insert", "update", "limit"):
        getattr(t, m).return_value = t
    t.execute.side_effect = list(resultados)
    return t


@pytest.fixture
def ctx(monkeypatch):
    tablas: dict[str, MagicMock] = {}
    db = MagicMock()
    db.table.side_effect = lambda nombre: tablas[nombre]
    llamadas: list[str] = []

    async def llm_falso(contexto: str, pregunta: str) -> str:
        llamadas.append(contexto)
        return "Resumen de la IA."

    monkeypatch.setattr(resumen, "ask_llm", llm_falso)
    app.dependency_overrides[get_current_user] = lambda: {"sub": AUTORA}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_consent] = lambda: None
    try:
        yield SimpleNamespace(client=TestClient(app), tablas=tablas, llamadas=llamadas)
    finally:
        app.dependency_overrides.clear()


def _contenido_final(ctx) -> str:
    """Contenido que la tarea en segundo plano escribió al completar."""
    return ctx.tablas["summaries"].update.call_args.args[0]["contenido"]


def _preparar(ctx, entradas, anuladas=(), revisiones=(), perfiles=()):
    ctx.tablas["summaries"] = _tabla(
        _ej([]),  # no hay resumen reciente que reutilizar
        _ej([_fila()]),  # insert en 'generando'
        _ej([_fila(estado="listo", contenido="x")]),  # update al completar
    )
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej(entradas), _ej(list(anuladas)))
    ctx.tablas["log_entry_revisions"] = _tabla(_ej(list(revisiones)))
    ctx.tablas["profiles"] = _tabla(_ej(list(perfiles)))


# --- Generación en segundo plano ---------------------------------------------


def test_post_responde_202_en_generando_y_completa_en_segundo_plano(ctx):
    _preparar(
        ctx,
        entradas=[
            {
                "id": E1,
                "occurred_at": "2026-10-12T17:30:00+00:00",
                "kind": "medicamento",
                "content": "Insulina 8 UI",
                "editada": False,
            }
        ],
        anuladas=[],
    )

    r = ctx.client.post(URL)

    assert r.status_code == 202
    assert r.json()["estado"] == "generando"
    insertada = ctx.tablas["summaries"].insert.call_args.args[0]
    assert insertada["estado"] == "generando"
    assert insertada["contenido"] is None
    # La tarea en segundo plano completó el resumen y lo marcó como listo.
    actualizada = ctx.tablas["summaries"].update.call_args.args[0]
    assert actualizada["estado"] == "listo"
    assert actualizada["contenido"].startswith("Resumen de la IA.")


def test_sin_correcciones_lo_declara_explicitamente(ctx):
    _preparar(
        ctx,
        entradas=[
            {
                "id": E1,
                "occurred_at": "2026-10-12T17:30:00+00:00",
                "kind": "medicamento",
                "content": "Insulina 8 UI",
                "editada": False,
            }
        ],
    )
    ctx.client.post(URL)

    contenido = _contenido_final(ctx)
    assert "generada por el sistema, no por IA" in contenido
    assert (
        "Entradas consideradas: 1 · Corregidas: 0 · Anuladas y excluidas del resumen: 0"
        in contenido
    )
    assert "No hubo correcciones ni anulaciones en el período." in contenido


def test_lista_correcciones_con_autor_fecha_y_conteo_y_cuenta_anuladas(ctx):
    _preparar(
        ctx,
        entradas=[
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
        ],
        anuladas=[{"id": "a1"}, {"id": "a2"}],
        revisiones=[
            {"entry_id": E1, "author_id": AUTORA, "created_at": "2026-10-12T19:05:00+00:00"},
            {"entry_id": E1, "author_id": AUTORA, "created_at": "2026-10-12T18:00:00+00:00"},
        ],
        perfiles=[{"id": AUTORA, "nombre": "Ana"}],
    )

    ctx.client.post(URL)

    contenido = _contenido_final(ctx)
    assert (
        "Entradas consideradas: 2 · Corregidas: 1 · Anuladas y excluidas del resumen: 2"
        in contenido
    )
    # Hora de Chile (UTC-3): 17:30 UTC -> 14:30, última corrección 19:05 UTC -> 16:05.
    assert (
        "- Entrada del 12-10-2026 14:30 (medicamento): corregida el 12-10-2026 16:05 "
        "por Ana (2 correcciones)." in contenido
    )
    # La IA recibe la versión vigente y sabe que fue corregida.
    assert "medicamento [corregida]: Insulina 6 UI" in ctx.llamadas[0]


def test_nota_no_expone_contenido_clinico_ni_motivos(ctx):
    _preparar(
        ctx,
        entradas=[
            {
                "id": E1,
                "occurred_at": "2026-10-12T17:30:00+00:00",
                "kind": "medicamento",
                "content": "Anticoagulante 5 mg",
                "editada": True,
            }
        ],
        revisiones=[
            {"entry_id": E1, "author_id": AUTORA, "created_at": "2026-10-12T19:05:00+00:00"}
        ],
        perfiles=[],  # sin nombre registrado
    )

    ctx.client.post(URL)

    nota = _contenido_final(ctx).split("---", 1)[1]
    assert "Anticoagulante" not in nota
    assert "por un miembro del grupo." in nota
    ctx.tablas["log_entry_revisions"].select.assert_called_with("entry_id, author_id, created_at")


def test_fallo_del_llm_marca_error_sin_contenido_y_no_loguea_el_texto(ctx, monkeypatch, caplog):
    async def llm_caido(contexto: str, pregunta: str) -> str:
        raise RuntimeError("cuota agotada con Juan Pérez: texto clínico")

    monkeypatch.setattr(resumen, "ask_llm", llm_caido)
    _preparar(ctx, entradas=[])

    r = ctx.client.post(URL)

    assert r.status_code == 202  # la respuesta ya salió; el fallo queda en el resumen
    actualizada = ctx.tablas["summaries"].update.call_args.args[0]
    assert actualizada == {"estado": "error"}
    assert "Juan Pérez" not in caplog.text
    assert "RuntimeError" in caplog.text


# --- Reutilización ------------------------------------------------------------


def test_reusa_resumen_listo_reciente_sin_llamar_al_llm(ctx):
    ctx.tablas["summaries"] = _tabla(
        _ej([_fila(estado="listo", contenido="ya listo", creado_hace=timedelta(hours=2))])
    )

    r = ctx.client.post(URL)

    assert r.status_code == 200
    assert r.json()["contenido"] == "ya listo"
    assert ctx.llamadas == []
    ctx.tablas["summaries"].insert.assert_not_called()


def test_reusa_resumen_en_curso_reciente(ctx):
    ctx.tablas["summaries"] = _tabla(
        _ej([_fila(estado="generando", creado_hace=timedelta(minutes=2))])
    )

    r = ctx.client.post(URL)

    assert r.status_code == 200
    assert r.json()["estado"] == "generando"
    ctx.tablas["summaries"].insert.assert_not_called()


def test_no_reusa_si_la_ventana_es_distinta(ctx):
    ctx.tablas["summaries"] = _tabla(
        _ej([_fila(estado="listo", dias=30, contenido="mensual", creado_hace=timedelta(hours=1))]),
        _ej([_fila(dias=7)]),  # insert de una nueva de 7 días
        _ej([_fila(estado="listo", contenido="x")]),
    )
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([]), _ej([]))
    ctx.tablas["log_entry_revisions"] = _tabla()
    ctx.tablas["profiles"] = _tabla()

    r = ctx.client.post(f"{URL}?dias=7")

    assert r.status_code == 202
    ctx.tablas["summaries"].insert.assert_called_once()


def test_forzar_ignora_el_resumen_reciente(ctx):
    # Con forzar no hay consulta de reutilización: lo primero es el insert.
    ctx.tablas["summaries"] = _tabla(
        _ej([_fila()]),  # insert
        _ej([_fila(estado="listo", contenido="x")]),  # update
    )
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([]), _ej([]))

    r = ctx.client.post(f"{URL}?forzar=true")

    assert r.status_code == 202
    ctx.tablas["summaries"].insert.assert_called_once()


def test_dias_fuera_de_rango_es_422(ctx):
    assert ctx.client.post(f"{URL}?dias=0").status_code == 422
    assert ctx.client.post(f"{URL}?dias=365").status_code == 422


# --- Consulta de estado -----------------------------------------------------


def test_obtener_resumen_devuelve_su_estado(ctx):
    ctx.tablas["summaries"] = _tabla(_ej([_fila(estado="listo", contenido="ok")]))

    r = ctx.client.get(f"{URL}/{RESUMEN}")

    assert r.status_code == 200
    assert r.json()["estado"] == "listo"


def test_obtener_resumen_inexistente_o_ajeno_es_404(ctx):
    ctx.tablas["summaries"] = _tabla(_ej([]))
    assert ctx.client.get(f"{URL}/{RESUMEN}").status_code == 404
