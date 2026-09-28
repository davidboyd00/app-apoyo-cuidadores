"""Tests del consentimiento versionado (Ley 21.719 · Punto 3).

Dos capas:
    1. RLS: consents es privado por usuario (SQL).
    2. HTTP: `require_consent` bloquea los routers de negocio con 409;
       los endpoints ARCO (`/me/*`) y `/centers` funcionan sin consent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.deps import get_current_user, get_db
from app.main import app

# ---------------------------------------------------------------------------
# Capa SQL — RLS de consents
# ---------------------------------------------------------------------------

pytestmark_rls = pytest.mark.rls


class TestConsentsRLS:
    pytestmark = pytest.mark.rls

    def test_usuario_solo_ve_sus_consents(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        a.sql("insert into consents (user_id, version) values (auth.uid(), '1.0')")
        b.sql("insert into consents (user_id, version) values (auth.uid(), '1.0')")

        rows_a = a.sql("select user_id from consents")
        rows_b = b.sql("select user_id from consents")
        assert len(rows_a) == 1 and str(rows_a[0][0]) == str(a.user_id)
        assert len(rows_b) == 1 and str(rows_b[0][0]) == str(b.user_id)

    def test_no_se_puede_insertar_consent_por_otro_usuario(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            a.sql("insert into consents (user_id, version) values (%s, '1.0')", (str(b.user_id),))

    def test_consents_es_append_only(self, make_user):
        a = make_user("a@test.cl")
        a.sql("insert into consents (user_id, version) values (auth.uid(), '1.0')")
        # RLS no tiene policy UPDATE ni DELETE → afecta 0 filas silenciosamente.
        a.sql("update consents set version = '2.0'")
        a.sql("delete from consents")
        rows = a.sql("select version from consents")
        assert len(rows) == 1 and rows[0][0] == "1.0"


# ---------------------------------------------------------------------------
# Capa HTTP — `require_consent` bloquea/desbloquea endpoints de negocio
# ---------------------------------------------------------------------------


@pytest.fixture
def http_client_no_consent():
    """Simula un usuario que NO tiene consent vigente."""
    fake_user = {"sub": "00000000-0000-0000-0000-000000000001"}

    def fake_db():
        m = MagicMock()
        # consents.select devuelve lista vacía → require_consent lanza 409.
        m.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        return m

    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = fake_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def http_client_con_consent():
    """Simula un usuario que sí aceptó la versión vigente."""
    fake_user = {"sub": "00000000-0000-0000-0000-000000000001"}

    def fake_db():
        m = MagicMock()
        # consents.select devuelve una fila → require_consent pasa.
        m.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"version": settings.policy_version}
        ]
        # cualquier otra .table().select()... devuelve data vacía por default.
        return m

    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = fake_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


class TestConsentGate:
    def test_endpoint_de_negocio_bloqueado_sin_consent(self, http_client_no_consent):
        r = http_client_no_consent.get("/patients")
        assert r.status_code == 409
        assert settings.policy_version in r.json()["detail"]
        assert "/me/consent" in r.json()["detail"]

    def test_endpoint_arco_no_requiere_consent(self, http_client_no_consent):
        # GET /me/consent debe funcionar aunque el user no haya aceptado nada.
        r = http_client_no_consent.get("/me/consent")
        assert r.status_code == 200
        body = r.json()
        assert body["version_vigente"] == settings.policy_version
        assert body["aceptada_en"] is None

    def test_centers_no_requiere_consent(self, http_client_no_consent):
        r = http_client_no_consent.get("/centers")
        assert r.status_code == 200

    def test_health_no_requiere_ni_auth_ni_consent(self):
        # Sin overrides → hitea la app tal cual.
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200

    def test_con_consent_pasa(self, http_client_con_consent):
        r = http_client_con_consent.get("/patients")
        assert r.status_code == 200

    def test_aceptar_version_incorrecta_es_409(self, http_client_no_consent):
        r = http_client_no_consent.post("/me/consent", json={"version": "9.9.9"})
        assert r.status_code == 409
        assert "vigente" in r.json()["detail"].lower()
