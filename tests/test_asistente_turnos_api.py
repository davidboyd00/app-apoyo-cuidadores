"""Endpoints nuevos del MVP: asistente estructurado, métrica de verificabilidad,
historial de una entrada y turnos de cuidado. La base es un doble (DBFalsa);
la autorización real se prueba en test_rls.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db, require_consent
from app.llm import LLMError, RespuestaAsistente
from app.main import app
from tests._db_falsa import DBFalsa

PACIENTE = "00000000-0000-0000-0000-0000000000aa"
USUARIA = "00000000-0000-0000-0000-000000000001"
OTRA = "00000000-0000-0000-0000-000000000002"
E1 = "00000000-0000-0000-0000-0000000000e1"
E2 = "00000000-0000-0000-0000-0000000000e2"
AHORA = datetime.now(UTC)


@pytest.fixture
def ctx():
    db = DBFalsa()
    app.dependency_overrides[get_current_user] = lambda: {"sub": USUARIA}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_consent] = lambda: None
    try:
        yield db, TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _entrada(id_, content="Durmió bien"):
    return {"id": id_, "occurred_at": AHORA.isoformat(), "kind": "nota", "content": content}


def _mensaje_guardado(pregunta="¿Cómo durmió?", cited=(), suficiente=True):
    return {
        "id": "00000000-0000-0000-0000-00000000aaaa",
        "patient_id": PACIENTE,
        "author_id": USUARIA,
        "conversation_id": "00000000-0000-0000-0000-00000000cccc",
        "pregunta": pregunta,
        "respuesta": "r",
        "cited_entry_ids": list(cited),
        "suficiente": suficiente,
        "created_at": AHORA.isoformat(),
    }


# --- Asistente ---


def test_asistente_guarda_citas_validadas_y_contexto(ctx, monkeypatch):
    db, client = ctx
    db.responder("log_entries_vigentes", "select", [_entrada(E1), _entrada(E2, "Comió poco")])
    db.responder("assistant_messages", "insert", [_mensaje_guardado(cited=[E1])])
    recibido = {}

    async def llm_falso(contexto, pregunta, ids_validos):
        recibido.update(contexto=contexto, ids=ids_validos)
        return RespuestaAsistente(respuesta="Durmió bien.", citas=[E1], descartadas=1)

    monkeypatch.setattr("app.routers.assistant.ask_llm_citado", llm_falso)
    r = client.post(f"/patients/{PACIENTE}/assistant/ask", json={"pregunta": "¿Cómo durmió?"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cited_entry_ids"] == [E1]
    assert body["suficiente"] is True
    assert recibido["ids"] == {E1, E2}
    assert f"[{E1}]" in recibido["contexto"]
    fila = db.operaciones("assistant_messages", "insert")[0].payload
    assert fila["cited_entry_ids"] == [E1]
    assert fila["contexto_entry_ids"] == [E1, E2]
    assert fila["suficiente"] is True
    # Auditoría: solo conteos, nunca texto.
    meta = db.operaciones("rpc:audit_write", "rpc")[0].payload["p_metadata"]
    assert meta["cited_count"] == 1 and meta["contexto_count"] == 2
    assert meta["citas_descartadas"] == 1
    assert "pregunta" not in meta and "respuesta" not in meta


def test_asistente_sin_registros_no_llama_al_llm(ctx, monkeypatch):
    db, client = ctx
    db.responder("assistant_messages", "insert", [_mensaje_guardado(suficiente=False)])

    async def no_deberia_llamarse(*_a, **_k):
        raise AssertionError("no se debe llamar al LLM sin registros")

    monkeypatch.setattr("app.routers.assistant.ask_llm_citado", no_deberia_llamarse)
    r = client.post(
        f"/patients/{PACIENTE}/assistant/ask", json={"pregunta": "¿Cómo durmió?", "ventana_dias": 7}
    )

    assert r.status_code == 200
    assert r.json()["suficiente"] is False
    assert r.json()["cited_entry_ids"] == []
    assert "últimos 7 días" in r.json()["respuesta"]


def test_asistente_caido_responde_503_sin_guardar(ctx, monkeypatch):
    db, client = ctx
    db.responder("log_entries_vigentes", "select", [_entrada(E1)])

    async def falla(*_a, **_k):
        raise LLMError("Gemini no devolvió texto (motivo: MAX_TOKENS)")

    monkeypatch.setattr("app.routers.assistant.ask_llm_citado", falla)
    r = client.post(f"/patients/{PACIENTE}/assistant/ask", json={"pregunta": "¿Cómo durmió?"})

    assert r.status_code == 503
    assert "no está disponible" in r.json()["detail"]
    assert db.operaciones("assistant_messages", "insert") == []


# --- Métrica de verificabilidad ---


def test_metrica_asistente_cuenta_citas_e_insuficientes(ctx):
    db, client = ctx
    db.responder(
        "assistant_messages",
        "select",
        [
            {"cited_entry_ids": [E1], "suficiente": True},
            {"cited_entry_ids": [], "suficiente": False},
            {"cited_entry_ids": [], "suficiente": True},
            {"cited_entry_ids": [E2], "suficiente": None},
        ],
    )
    r = client.get(f"/patients/{PACIENTE}/metrics/asistente")
    assert r.status_code == 200
    assert r.json()["total_respuestas"] == 4
    assert r.json()["verificables"] == 3
    assert r.json()["porcentaje_verificable"] == 75.0
    assert r.json()["cumple_kpi"] is False


def test_metrica_asistente_sin_respuestas_no_cumple(ctx):
    _, client = ctx
    r = client.get(f"/patients/{PACIENTE}/metrics/asistente")
    assert r.json()["total_respuestas"] == 0
    assert r.json()["cumple_kpi"] is False


# --- Historial de una entrada ---


def test_historial_trae_original_y_revisiones(ctx):
    db, client = ctx
    original = {
        "id": E1,
        "patient_id": PACIENTE,
        "author_id": USUARIA,
        "kind": "nota",
        "content": "Texto original",
        "occurred_at": AHORA.isoformat(),
        "source": "text",
        "created_at": AHORA.isoformat(),
        "registrado_en": None,
    }
    revision = {
        "id": "00000000-0000-0000-0000-0000000000b1",
        "entry_id": E1,
        "patient_id": PACIENTE,
        "author_id": USUARIA,
        "accion": "editar",
        "motivo": "Hora equivocada",
        "kind": "nota",
        "content": "Texto corregido",
        "occurred_at": AHORA.isoformat(),
        "created_at": AHORA.isoformat(),
    }
    db.responder("log_entries", "select", [original])
    db.responder("log_entry_revisions", "select", [revision])
    r = client.get(f"/patients/{PACIENTE}/entries/{E1}/historial")
    assert r.status_code == 200
    assert r.json()["original"]["content"] == "Texto original"
    assert [x["motivo"] for x in r.json()["revisiones"]] == ["Hora equivocada"]


def test_historial_inexistente_es_404(ctx):
    _, client = ctx
    r = client.get(f"/patients/{PACIENTE}/entries/{E1}/historial")
    assert r.status_code == 404
    assert "no encontrada" in r.json()["detail"]


# --- Turnos ---


def _turno(cuidador=USUARIA, horas=8):
    inicio = AHORA.replace(microsecond=0)
    return {
        "id": "00000000-0000-0000-0000-0000000000d1",
        "patient_id": PACIENTE,
        "cuidador_id": cuidador,
        "inicio": inicio.isoformat(),
        "fin": (inicio + timedelta(hours=horas)).isoformat(),
        "creado_por": USUARIA,
        "created_at": AHORA.isoformat(),
    }


def test_turno_sin_cuidador_es_de_quien_lo_crea(ctx):
    db, client = ctx
    db.responder("care_shifts", "insert", [_turno()])
    inicio = AHORA.replace(microsecond=0)
    r = client.post(
        f"/patients/{PACIENTE}/turnos",
        json={"inicio": inicio.isoformat(), "fin": (inicio + timedelta(hours=8)).isoformat()},
    )
    assert r.status_code == 201, r.text
    assert db.operaciones("care_shifts", "insert")[0].payload["cuidador_id"] == USUARIA


def test_turno_para_otra_persona(ctx):
    db, client = ctx
    db.responder("care_shifts", "insert", [_turno(cuidador=OTRA)])
    inicio = AHORA.replace(microsecond=0)
    r = client.post(
        f"/patients/{PACIENTE}/turnos",
        json={
            "cuidador_id": OTRA,
            "inicio": inicio.isoformat(),
            "fin": (inicio + timedelta(hours=4)).isoformat(),
        },
    )
    assert r.status_code == 201
    assert db.operaciones("care_shifts", "insert")[0].payload["cuidador_id"] == OTRA


@pytest.mark.parametrize(
    ("horas", "mensaje"),
    [(-1, "terminar después de empezar"), (0, "terminar después de empezar"), (24 * 8, "7 días")],
)
def test_turno_con_rango_invalido_es_422_en_espanol(ctx, horas, mensaje):
    db, client = ctx
    inicio = AHORA.replace(microsecond=0)
    r = client.post(
        f"/patients/{PACIENTE}/turnos",
        json={"inicio": inicio.isoformat(), "fin": (inicio + timedelta(hours=horas)).isoformat()},
    )
    assert r.status_code == 422
    assert mensaje in r.text
    assert db.operaciones("care_shifts", "insert") == []


def test_turno_sin_zona_horaria_es_422(ctx):
    _, client = ctx
    r = client.post(
        f"/patients/{PACIENTE}/turnos",
        json={"inicio": "2026-11-21T08:00:00", "fin": "2026-11-21T16:00:00"},
    )
    assert r.status_code == 422


def test_listar_turnos_filtra_por_cruce_de_ventana(ctx):
    db, client = ctx
    db.responder("care_shifts", "select", [_turno()])
    r = client.get(f"/patients/{PACIENTE}/turnos")
    assert r.status_code == 200
    filtros = dict(db.operaciones("care_shifts", "select")[0].filtros)
    assert "lt" in filtros and filtros["lt"][0] == "inicio"
    assert "gt" in filtros and filtros["gt"][0] == "fin"


def test_listar_turnos_ventana_muy_larga_es_400(ctx):
    _, client = ctx
    desde = AHORA - timedelta(days=40)
    hasta = AHORA + timedelta(days=40)
    r = client.get(
        f"/patients/{PACIENTE}/turnos",
        params={"desde": desde.isoformat(), "hasta": hasta.isoformat()},
    )
    assert r.status_code == 400
    assert "62 días" in r.json()["detail"]


def test_eliminar_turno_ajeno_o_inexistente_es_404(ctx):
    _, client = ctx
    r = client.delete(f"/patients/{PACIENTE}/turnos/00000000-0000-0000-0000-0000000000d1")
    assert r.status_code == 404


def test_eliminar_turno_propio_es_204(ctx):
    db, client = ctx
    db.responder("care_shifts", "delete", [_turno()])
    r = client.delete(f"/patients/{PACIENTE}/turnos/00000000-0000-0000-0000-0000000000d1")
    assert r.status_code == 204
