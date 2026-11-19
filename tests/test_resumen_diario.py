"""Cron diario de resúmenes (scripts/resumen_diario.py).

Excepción documentada a la regla de scripts/: lee bitácora vigente con la
service role. Estos tests fijan lo que el script SÍ puede hacer: leer
entradas vigentes, insertar y completar filas de `summaries`. Nada más.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import resumen_diario  # noqa: E402

AHORA = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)
P1 = "00000000-0000-0000-0000-0000000000a1"
P2 = "00000000-0000-0000-0000-0000000000a2"
RES = "00000000-0000-0000-0000-0000000000ff"


def _ej(data):
    return SimpleNamespace(data=data)


def _tabla(*resultados):
    t = MagicMock()
    for m in ("select", "eq", "gte", "in_", "insert", "update", "limit"):
        getattr(t, m).return_value = t
    t.execute.side_effect = list(resultados)
    return t


@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setenv("RESUMEN_PAUSA_SEG", "0")
    tablas: dict[str, MagicMock] = {}
    db = MagicMock()
    db.table.side_effect = lambda nombre: tablas[nombre]
    llamadas: list[str] = []

    async def llm_falso(contexto: str, pregunta: str) -> str:
        llamadas.append(contexto)
        return "Resumen."

    monkeypatch.setattr(resumen_diario, "construir_resumen", _construir_falso(llm_falso))
    return SimpleNamespace(db=db, tablas=tablas, llamadas=llamadas)


def _construir_falso(llm):
    """Sustituye construir_resumen por una versión que no toca la base real."""

    async def construir(db, patient_id, desde, hasta):
        await llm("", "")
        return f"contenido de {patient_id}"

    return construir


def test_genera_un_resumen_por_paciente_con_actividad(cliente):
    cliente.tablas["log_entries_vigentes"] = _tabla(_ej([{"patient_id": P1}, {"patient_id": P2}]))
    cliente.tablas["summaries"] = _tabla(
        _ej([]),
        _ej([{"id": RES}]),
        _ej([]),  # P1: sin previo, insert, update
        _ej([]),
        _ej([{"id": RES}]),
        _ej([]),  # P2: idem
    )

    import asyncio

    conteo = asyncio.run(resumen_diario.generar_diario(cliente.db, AHORA))

    assert conteo == {"pacientes": 2, "omitidos": 0, "listos": 2, "errores": 0}
    insertadas = cliente.tablas["summaries"].insert.call_args_list
    assert all(c.args[0]["estado"] == "generando" for c in insertadas)
    assert all("author_id" not in c.args[0] for c in insertadas)  # sin usuario: queda nulo


def test_omite_pacientes_ya_resumidos_hoy(cliente):
    cliente.tablas["log_entries_vigentes"] = _tabla(_ej([{"patient_id": P1}]))
    cliente.tablas["summaries"] = _tabla(_ej([{"id": "ya-existe"}]))  # hay uno de hoy

    import asyncio

    conteo = asyncio.run(resumen_diario.generar_diario(cliente.db, AHORA))

    assert conteo["omitidos"] == 1 and conteo["listos"] == 0
    cliente.tablas["summaries"].insert.assert_not_called()
    assert cliente.llamadas == []  # no se gasta cuota del LLM


def test_fallo_del_llm_marca_error_y_no_loguea_contenido(cliente, monkeypatch, caplog):
    async def caido(db, patient_id, desde, hasta):
        raise RuntimeError("Juan Pérez con diagnóstico X")

    monkeypatch.setattr(resumen_diario, "construir_resumen", caido)
    cliente.tablas["log_entries_vigentes"] = _tabla(_ej([{"patient_id": P1}]))
    cliente.tablas["summaries"] = _tabla(_ej([]), _ej([{"id": RES}]), _ej([]))

    import asyncio

    conteo = asyncio.run(resumen_diario.generar_diario(cliente.db, AHORA))

    assert conteo["errores"] == 1
    assert cliente.tablas["summaries"].update.call_args.args[0] == {"estado": "error"}
    assert "Juan Pérez" not in caplog.text and "diagnóstico" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_solo_escribe_en_summaries(cliente):
    """Ni entradas, ni revisiones ni perfiles se modifican desde el cron."""
    cliente.tablas["log_entries_vigentes"] = _tabla(_ej([{"patient_id": P1}]))
    cliente.tablas["summaries"] = _tabla(_ej([]), _ej([{"id": RES}]), _ej([]))

    import asyncio

    asyncio.run(resumen_diario.generar_diario(cliente.db, AHORA))

    usadas = {c.args[0] for c in cliente.db.table.call_args_list}
    assert usadas == {"log_entries_vigentes", "summaries"}
    for nombre in ("log_entries", "log_entry_revisions", "profiles"):
        assert nombre not in usadas
