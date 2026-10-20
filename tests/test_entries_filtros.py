"""Filtros del feed de bitácora: `kind` y `antes` en GET /patients/{id}/entries.

No requieren Postgres ni Supabase: `get_db` devuelve un query builder falso
que registra las llamadas, así verificamos que el filtro se envía a la base
(y no se filtra en memoria) y que `limit`/`desde` siguen igual.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db, require_consent
from app.main import app

USER = "00000000-0000-0000-0000-000000000001"
PACIENTE = "00000000-0000-0000-0000-0000000000aa"
URL = f"/patients/{PACIENTE}/entries"


class QueryFalsa:
    """Imita el builder encadenable de postgrest y anota cada llamada."""

    def __init__(self):
        self.llamadas: list[tuple[str, tuple]] = []

    def __getattr__(self, nombre):
        def metodo(*args, **_kwargs):
            self.llamadas.append((nombre, args))
            return self

        return metodo

    def execute(self):
        return MagicMock(data=[])

    def filtros(self, nombre: str) -> list[tuple]:
        return [args for n, args in self.llamadas if n == nombre]


@pytest.fixture
def query():
    q = QueryFalsa()
    db = MagicMock()
    db.table.return_value = q
    app.dependency_overrides[get_current_user] = lambda: {"sub": USER}
    app.dependency_overrides[get_db] = lambda: db
    # El gate de consentimiento también consulta la base: lo dejamos pasar.
    app.dependency_overrides[require_consent] = lambda: None
    try:
        yield q
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(query):
    return TestClient(app)


def test_sin_filtros_nuevos_el_comportamiento_es_el_de_siempre(client, query):
    r = client.get(URL, params={"limit": 20})
    assert r.status_code == 200
    assert ("kind", "nota") not in query.filtros("eq")
    assert query.filtros("lt") == []
    assert query.filtros("limit") == [(20,)]
    assert query.filtros("order") == [("occurred_at",)]


def test_kind_filtra_en_el_servidor(client, query):
    r = client.get(URL, params={"kind": "medicamento"})
    assert r.status_code == 200
    assert ("kind", "medicamento") in query.filtros("eq")
    # El filtro de paciente sigue presente.
    assert ("patient_id", PACIENTE) in query.filtros("eq")


def test_kind_invalido_es_422_en_espanol(client):
    r = client.get(URL, params={"kind": "invento"})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "Datos de la solicitud inválidos"
    assert any(e["campo"] == "kind" for e in body["errores"])


def test_antes_pagina_con_lt_sobre_occurred_at(client, query):
    r = client.get(URL, params={"antes": "2026-10-01T12:00:00+00:00"})
    assert r.status_code == 200
    assert query.filtros("lt") == [("occurred_at", "2026-10-01T12:00:00+00:00")]


def test_antes_invalido_es_422(client):
    r = client.get(URL, params={"antes": "ayer"})
    assert r.status_code == 422
    assert any(e["campo"] == "antes" for e in r.json()["errores"])


def test_desde_y_filtros_nuevos_se_combinan(client, query):
    r = client.get(
        URL,
        params={
            "desde": "2026-09-01T00:00:00+00:00",
            "antes": "2026-10-01T00:00:00+00:00",
            "kind": "sintoma",
        },
    )
    assert r.status_code == 200
    assert query.filtros("gte") == [("occurred_at", "2026-09-01T00:00:00+00:00")]
    assert query.filtros("lt") == [("occurred_at", "2026-10-01T00:00:00+00:00")]
    assert ("kind", "sintoma") in query.filtros("eq")
