"""Validación de entradas de bitácora (cambio de contrato).

- `content` no puede ser vacío (crear y editar).
- `occurred_at` debe traer zona horaria (crear y editar).
- Los 422 dicen en español qué falta y en qué campo.
- La respuesta (`Entry`) no aplica esas reglas: filas históricas con
  content vacío se siguen pudiendo leer.

No requieren Postgres ni Supabase: get_db es un MagicMock.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db, require_consent
from app.main import app
from app.schemas import Entry

PACIENTE = "00000000-0000-0000-0000-0000000000aa"
ENTRADA = "00000000-0000-0000-0000-0000000000bb"
URL = f"/patients/{PACIENTE}/entries"


FILA = {
    "id": ENTRADA,
    "patient_id": PACIENTE,
    "author_id": "00000000-0000-0000-0000-000000000001",
    "kind": "nota",
    "content": "x",
    "occurred_at": "2026-10-19T18:30:00+00:00",
    "source": "text",
    "created_at": "2026-10-19T18:30:01+00:00",
}


@pytest.fixture
def client():
    db = MagicMock()
    # El insert "devuelve" una fila válida para poder verificar el 201.
    db.table.return_value.insert.return_value.execute.return_value.data = [FILA]
    app.dependency_overrides[get_current_user] = lambda: {"sub": "u1"}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_consent] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _errores(r) -> dict[str, str]:
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "Datos de la solicitud inválidos"
    return {e["campo"]: e["msg"] for e in body["errores"]}


def test_crear_sin_zona_horaria_es_422_en_espanol(client):
    r = client.post(
        URL, json={"kind": "nota", "content": "x", "occurred_at": "2026-10-19T15:30:00"}
    )
    errores = _errores(r)
    assert errores["occurred_at"].startswith("occurred_at debe incluir zona horaria")


def test_crear_con_content_vacio_es_422_en_espanol(client):
    r = client.post(
        URL, json={"kind": "nota", "content": "", "occurred_at": "2026-10-19T15:30:00-03:00"}
    )
    assert _errores(r)["content"] == "content no puede estar vacío"


REVISION = f"{URL}/{ENTRADA}/revisiones"
BASE_REVISION = {"motivo": "error de dosis", "kind": "nota", "content": "corregido"}


def test_revisar_sin_zona_horaria_es_422(client):
    r = client.post(REVISION, json=BASE_REVISION | {"occurred_at": "2026-10-19T15:30:00"})
    assert "occurred_at" in _errores(r)


def test_revisar_con_content_vacio_es_422(client):
    r = client.post(
        REVISION, json=BASE_REVISION | {"content": "", "occurred_at": "2026-10-19T15:30:00-03:00"}
    )
    assert _errores(r)["content"] == "content no puede estar vacío"


def test_revisar_con_content_solo_espacios_es_422(client):
    r = client.post(
        REVISION,
        json=BASE_REVISION | {"content": "   ", "occurred_at": "2026-10-19T15:30:00-03:00"},
    )
    assert "content" in _errores(r)


def test_revisar_sin_motivo_es_422(client):
    r = client.post(
        REVISION,
        json={"kind": "nota", "content": "x", "occurred_at": "2026-10-19T15:30:00-03:00"},
    )
    assert "motivo" in _errores(r)


def test_motivo_de_solo_espacios_es_422(client):
    r = client.post(
        REVISION,
        json=BASE_REVISION | {"motivo": "     ", "occurred_at": "2026-10-19T15:30:00-03:00"},
    )
    assert (
        _errores(r)["motivo"] == "El motivo debe tener al menos 3 caracteres (sin contar espacios)"
    )


def test_anular_sin_motivo_es_422(client):
    r = client.post(f"{URL}/{ENTRADA}/anulacion", json={})
    assert "motivo" in _errores(r)


def test_patch_y_delete_ya_no_existen(client):
    """La bitácora es insert-only: no hay edición ni borrado directo sobre la
    entrada. Las correcciones van por POST .../revisiones y .../anulacion."""
    assert client.patch(f"{URL}/{ENTRADA}", json={"content": "x"}).status_code == 404
    assert client.delete(f"{URL}/{ENTRADA}").status_code == 404


@pytest.mark.parametrize(
    "fecha", ["2026-10-19T15:30:00Z", "2026-10-19T15:30:00-03:00", "2026-10-19T18:30:00+00:00"]
)
def test_fechas_con_zona_se_aceptan(client, fecha):
    r = client.post(URL, json={"kind": "nota", "content": "x", "occurred_at": fecha})
    assert r.status_code == 201


def test_respuesta_acepta_filas_historicas_con_content_vacio():
    assert Entry.model_validate({**FILA, "content": ""}).content == ""
