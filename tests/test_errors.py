"""Tests de manejo de errores.

Cubren el contrato de respuestas de error: códigos correctos, mensajes en
español y formato consistente para el front (Desarrollo Móvil).

No requieren Postgres ni Supabase: mockeamos `get_db` con dependency_overrides
y `get_current_user` — solo validamos la capa HTTP.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app.deps import get_current_user, get_db
from app.main import app


@pytest.fixture
def client():
    fake_user = {"sub": "00000000-0000-0000-0000-000000000001"}
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


class TestValidacionRequest:
    def test_uuid_malformado_en_path_devuelve_422_en_espanol(self, client):
        r = client.get("/patients/no-es-uuid/entries")
        assert r.status_code == 422
        body = r.json()
        assert body["detail"] == "Datos de la solicitud inválidos"
        assert any("UUID" in e["msg"] for e in body["errores"])
        assert any(e["campo"] == "patient_id" for e in body["errores"])

    def test_payload_incompleto_devuelve_422_con_campos(self, client):
        r = client.post(
            "/patients/00000000-0000-0000-0000-000000000001/entries",
            json={"kind": "nota"},  # faltan content y occurred_at
        )
        assert r.status_code == 422
        body = r.json()
        campos = {e["campo"] for e in body["errores"]}
        assert "content" in campos
        assert "occurred_at" in campos

    def test_enum_no_permitido_devuelve_422(self, client):
        r = client.post(
            "/patients/00000000-0000-0000-0000-000000000001/entries",
            json={"kind": "invento", "content": "x", "occurred_at": "2026-09-10T12:00:00Z"},
        )
        assert r.status_code == 422

    def test_ventana_metrica_invalida_devuelve_400_en_espanol(self, client):
        r = client.get(
            "/patients/00000000-0000-0000-0000-000000000001/metrics/relevo",
            params={"desde": "2026-01-10T00:00:00Z", "hasta": "2026-01-01T00:00:00Z"},
        )
        assert r.status_code == 400
        assert "desde" in r.json()["detail"].lower()


class TestErroresPostgREST:
    def _apierror(self, code: str, message: str = "") -> APIError:
        return APIError({"code": code, "message": message, "hint": None, "details": None})

    def test_permiso_denegado_se_mapea_a_403(self, client):
        def db_boom():
            m = MagicMock()
            m.table.side_effect = self._apierror("42501", "permission denied")
            return m

        app.dependency_overrides[get_db] = db_boom

        r = client.get("/patients")
        assert r.status_code == 403
        assert "permisos" in r.json()["detail"].lower()

    def test_unique_violation_se_mapea_a_409(self, client):
        def db_boom():
            m = MagicMock()
            m.table.side_effect = self._apierror("23505", "duplicate key")
            return m

        app.dependency_overrides[get_db] = db_boom

        r = client.post("/patients", json={"nombre": "X"})
        assert r.status_code == 409

    def test_error_desconocido_se_mapea_a_500_generico(self, client):
        def db_boom():
            m = MagicMock()
            m.table.side_effect = self._apierror("XX999", "algo raro")
            return m

        app.dependency_overrides[get_db] = db_boom

        r = client.get("/patients")
        assert r.status_code == 500
        assert "algo raro" not in r.json()["detail"]  # no filtramos detalle interno
