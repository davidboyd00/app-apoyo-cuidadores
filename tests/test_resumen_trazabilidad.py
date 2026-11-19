"""Resúmenes médicos: generación en segundo plano, reutilización, trazabilidad,
límites de contexto y medición de tiempos.

La nota de trazabilidad la arma el código, no el LLM. Todos sus datos salen de
UNA consulta a `log_entries_vigentes`; solo se consulta `profiles` para
nombres, y solo si hubo correcciones.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import resumen
from app.config import settings
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


def _entrada(
    id_,
    occurred_at,
    kind,
    content,
    editada=False,
    anulada=False,
    revisiones=0,
    ultima_at=None,
    autor=None,
):
    return {
        "id": id_,
        "occurred_at": occurred_at,
        "kind": kind,
        "content": content,
        "editada": editada,
        "anulada": anulada,
        "revisiones_count": revisiones,
        "ultima_revision_at": ultima_at,
        "ultima_revision_autor_id": autor,
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
    return ctx.tablas["summaries"].update.call_args.args[0]["contenido"]


def _preparar(ctx, entradas, perfiles=()):
    """Secuencia de la ruta: reutilización (vacía) -> insert -> consulta de
    entradas -> [perfiles si hubo correcciones] -> update final."""
    ctx.tablas["summaries"] = _tabla(
        _ej([]),
        _ej([_fila()]),
        _ej([_fila(estado="listo", contenido="x")]),
    )
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej(entradas))
    ctx.tablas["profiles"] = _tabla(_ej(list(perfiles)))


# --- Generación en segundo plano ---------------------------------------------


def test_post_responde_202_en_generando_y_completa_en_segundo_plano(ctx):
    _preparar(ctx, [_entrada(E1, "2026-10-12T17:30:00+00:00", "medicamento", "Insulina 8 UI")])

    r = ctx.client.post(URL)

    assert r.status_code == 202
    assert r.json()["estado"] == "generando"
    assert ctx.tablas["summaries"].insert.call_args.args[0]["contenido"] is None
    actualizada = ctx.tablas["summaries"].update.call_args.args[0]
    assert actualizada["estado"] == "listo"
    assert actualizada["contenido"].startswith("Resumen de la IA.")


def test_sin_correcciones_lo_declara_explicitamente(ctx):
    _preparar(ctx, [_entrada(E1, "2026-10-12T17:30:00+00:00", "medicamento", "Insulina 8 UI")])

    ctx.client.post(URL)

    contenido = _contenido_final(ctx)
    assert "generada por el sistema, no por IA" in contenido
    assert (
        "Entradas consideradas: 1 · Corregidas: 0 · Anuladas y excluidas del resumen: 0"
        in contenido
    )
    assert "No hubo correcciones ni anulaciones en el período." in contenido
    # Sin correcciones no se consultan perfiles.
    ctx.tablas["profiles"].select.assert_not_called()


def test_lista_correcciones_con_autor_fecha_conteo_y_anuladas(ctx):
    _preparar(
        ctx,
        [
            _entrada(
                E1,
                "2026-10-12T17:30:00+00:00",
                "medicamento",
                "Insulina 6 UI",
                editada=True,
                revisiones=2,
                ultima_at="2026-10-12T19:05:00+00:00",
                autor=AUTORA,
            ),
            _entrada(E2, "2026-10-11T12:00:00+00:00", "nota", "Durmió bien"),
            _entrada("a1", "2026-10-10T10:00:00+00:00", "nota", "x", anulada=True),
            _entrada("a2", "2026-10-09T10:00:00+00:00", "nota", "y", anulada=True),
        ],
        perfiles=[{"id": AUTORA, "nombre": "Ana"}],
    )

    ctx.client.post(URL)

    contenido = _contenido_final(ctx)
    assert (
        "Entradas consideradas: 2 · Corregidas: 1 · Anuladas y excluidas del resumen: 2"
        in contenido
    )
    # 17:30 UTC -> 14:30 Chile; última corrección 19:05 UTC -> 16:05.
    assert (
        "- Entrada del 12-10-2026 14:30 (medicamento): corregida el 12-10-2026 16:05 "
        "por Ana (2 correcciones)." in contenido
    )
    assert "medicamento [corregida]: Insulina 6 UI" in ctx.llamadas[0]


def test_anuladas_no_llegan_a_la_ia(ctx):
    _preparar(
        ctx,
        [
            _entrada(E1, "2026-10-12T17:30:00+00:00", "medicamento", "Dato válido"),
            _entrada("a1", "2026-10-11T10:00:00+00:00", "nota", "REGISTRO ERRADO", anulada=True),
        ],
    )
    ctx.client.post(URL)
    assert "REGISTRO ERRADO" not in ctx.llamadas[0]


def test_nota_no_expone_contenido_clinico_ni_motivos(ctx):
    _preparar(
        ctx,
        [
            _entrada(
                E1,
                "2026-10-12T17:30:00+00:00",
                "medicamento",
                "Anticoagulante 5 mg",
                editada=True,
                revisiones=1,
                ultima_at="2026-10-12T19:05:00+00:00",
                autor=AUTORA,
            )
        ],
        perfiles=[],
    )

    ctx.client.post(URL)

    nota = _contenido_final(ctx).split("---", 1)[1]
    assert "Anticoagulante" not in nota
    assert "por un miembro del grupo." in nota


def test_fallo_del_llm_marca_error_sin_contenido_y_no_loguea_el_texto(ctx, monkeypatch, caplog):
    async def llm_caido(contexto: str, pregunta: str) -> str:
        raise RuntimeError("cuota agotada con Juan Pérez: texto clínico")

    monkeypatch.setattr(resumen, "ask_llm", llm_caido)
    _preparar(ctx, [])

    r = ctx.client.post(URL)

    assert r.status_code == 202
    assert ctx.tablas["summaries"].update.call_args.args[0] == {"estado": "error"}
    assert "Juan Pérez" not in caplog.text
    assert "RuntimeError" in caplog.text


# --- Una sola consulta -------------------------------------------------------


def test_la_nota_sale_de_una_sola_consulta_a_la_vista(ctx):
    _preparar(
        ctx,
        [
            _entrada(
                E1,
                "2026-10-12T17:30:00+00:00",
                "medicamento",
                "x",
                editada=True,
                revisiones=3,
                ultima_at="2026-10-12T19:05:00+00:00",
                autor=AUTORA,
            ),
            _entrada("a1", "2026-10-11T10:00:00+00:00", "nota", "y", anulada=True),
        ],
        perfiles=[{"id": AUTORA, "nombre": "Ana"}],
    )

    ctx.client.post(URL)

    # Una consulta para entradas (incluye anuladas) y ninguna a revisiones.
    assert ctx.tablas["log_entries_vigentes"].select.call_count == 1
    assert "log_entry_revisions" not in ctx.tablas  # nunca se consulta
    assert "(3 correcciones)" in _contenido_final(ctx)


# --- Límites de contexto ------------------------------------------------------


def test_contexto_se_acota_a_las_entradas_mas_recientes(ctx, monkeypatch):
    monkeypatch.setattr(settings, "resumen_max_entradas", 2)
    _preparar(
        ctx,
        [
            _entrada(E1, "2026-10-12T10:00:00+00:00", "nota", "reciente-1"),
            _entrada(E2, "2026-10-11T10:00:00+00:00", "nota", "reciente-2"),
            _entrada("e3", "2026-10-10T10:00:00+00:00", "nota", "antigua"),
        ],
    )

    ctx.client.post(URL)

    assert "reciente-1" in ctx.llamadas[0] and "reciente-2" in ctx.llamadas[0]
    assert "antigua" not in ctx.llamadas[0]
    assert "se generó con las 2 entradas más recientes de 3" in _contenido_final(ctx)


def test_contexto_se_acota_por_caracteres(ctx, monkeypatch):
    monkeypatch.setattr(settings, "resumen_max_caracteres", 120)
    _preparar(
        ctx,
        [
            _entrada(E1, "2026-10-12T10:00:00+00:00", "nota", "a" * 80),
            _entrada(E2, "2026-10-11T10:00:00+00:00", "nota", "b" * 80),
        ],
    )

    ctx.client.post(URL)

    assert len(ctx.llamadas[0]) <= 120
    assert "b" * 10 not in ctx.llamadas[0]


def test_sin_truncar_no_se_menciona_el_limite(ctx):
    _preparar(ctx, [_entrada(E1, "2026-10-12T10:00:00+00:00", "nota", "corta")])
    ctx.client.post(URL)
    assert "por límite de tamaño" not in _contenido_final(ctx)


# --- Medición -----------------------------------------------------------------


def test_mide_cada_etapa_sin_registrar_contenido(ctx, caplog):
    caplog.set_level(logging.INFO, logger="resumen")
    _preparar(ctx, [_entrada(E1, "2026-10-12T10:00:00+00:00", "nota", "texto clínico secreto")])

    ctx.client.post(URL)

    tiempos = [r.getMessage() for r in caplog.records if r.name == "resumen"]
    for etapa in ("consulta", "llm", "nota"):
        assert any(f"resumen_tiempo etapa={etapa} ms=" in m for m in tiempos), etapa
    assert "texto clínico secreto" not in caplog.text


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
        _ej([_fila(dias=7)]),
        _ej([_fila(estado="listo", contenido="x")]),
    )
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([]))

    r = ctx.client.post(f"{URL}?dias=7")

    assert r.status_code == 202
    ctx.tablas["summaries"].insert.assert_called_once()


def test_forzar_ignora_el_resumen_reciente(ctx):
    ctx.tablas["summaries"] = _tabla(
        _ej([_fila()]),
        _ej([_fila(estado="listo", contenido="x")]),
    )
    ctx.tablas["log_entries_vigentes"] = _tabla(_ej([]))

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
