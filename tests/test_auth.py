"""Verificación de JWT en `get_current_user`.

No requieren Postgres ni Supabase: generamos una llave EC P-256 local,
mockeamos el fetch del JWKS y firmamos tokens como lo haría Supabase Auth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.error import URLError

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from jose import jwk, jwt

from app import deps
from app.config import settings

KID = "llave-de-prueba"
SECRETO_HS256 = "secreto-legacy-de-prueba"


@pytest.fixture(scope="module")
def llave_ec():
    privada = ec.generate_private_key(ec.SECP256R1())
    pem_privada = privada.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pem_publica = (
        privada.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    publica_jwk = jwk.construct(pem_publica, "ES256").to_dict()
    publica_jwk.update({"kid": KID, "alg": "ES256", "use": "sig"})
    return {"privada": pem_privada, "publica": pem_publica, "jwk": publica_jwk}


@pytest.fixture(autouse=True)
def jwks_mock(monkeypatch, llave_ec):
    """JWKS falso + caché limpia en cada test; cuenta los fetches."""
    llamadas = {"n": 0}

    def fake_fetch():
        llamadas["n"] += 1
        return [llave_ec["jwk"]]

    monkeypatch.setattr(deps, "_fetch_jwks", fake_fetch)
    monkeypatch.setattr(deps, "_jwks_cache", {"keys": [], "fetched_at": float("-inf")})
    monkeypatch.setattr(settings, "supabase_jwt_secret", None)
    return llamadas


def _claims(**extra) -> dict:
    ahora = int(time.time())
    return {
        "sub": "11111111-1111-1111-1111-111111111111",
        "aud": "authenticated",
        "role": "authenticated",
        "iat": ahora,
        "exp": ahora + 3600,
        **extra,
    }


def _es256(llave_ec, kid: str = KID, **extra) -> str:
    return jwt.encode(
        _claims(**extra), llave_ec["privada"], algorithm="ES256", headers={"kid": kid}
    )


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def _token_a_mano(header: dict, secreto: bytes | None) -> str:
    """Arma un JWT sin validaciones de librería (para simular ataques)."""
    cuerpo = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(_claims()).encode())}"
    if secreto is None:
        return f"{cuerpo}."
    firma = hmac.new(secreto, cuerpo.encode(), hashlib.sha256).digest()
    return f"{cuerpo}.{_b64(firma)}"


def _assert_401(token: str):
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(token)
    assert exc.value.status_code == 401


def test_es256_valido_devuelve_claims(llave_ec):
    payload = deps.get_current_user(_es256(llave_ec))
    assert payload["sub"] == "11111111-1111-1111-1111-111111111111"


def test_es256_expirado_es_401(llave_ec):
    _assert_401(_es256(llave_ec, exp=int(time.time()) - 10))


def test_es256_audiencia_incorrecta_es_401(llave_ec):
    _assert_401(_es256(llave_ec, aud="anon"))


def test_es256_firmado_con_otra_llave_es_401():
    otra = ec.generate_private_key(ec.SECP256R1())
    pem = otra.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    _assert_401(jwt.encode(_claims(), pem, algorithm="ES256", headers={"kid": KID}))


def test_jwks_se_cachea(llave_ec, jwks_mock):
    deps.get_current_user(_es256(llave_ec))
    deps.get_current_user(_es256(llave_ec))
    assert jwks_mock["n"] == 1


def test_kid_desconocido_es_401_y_no_martilla_jwks(llave_ec, jwks_mock):
    deps.get_current_user(_es256(llave_ec))  # llena la caché
    _assert_401(_es256(llave_ec, kid="inventado-1"))
    _assert_401(_es256(llave_ec, kid="inventado-2"))
    # Caché recién llenada: los kid inventados no fuerzan nuevos fetches.
    assert jwks_mock["n"] == 1


def test_jwks_inalcanzable_es_503(llave_ec, monkeypatch):
    def caido():
        raise URLError("sin red")

    monkeypatch.setattr(deps, "_fetch_jwks", caido)
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(_es256(llave_ec))
    assert exc.value.status_code == 503


def test_hs256_legacy_con_secreto(monkeypatch):
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRETO_HS256)
    token = jwt.encode(_claims(), SECRETO_HS256, algorithm="HS256")
    assert deps.get_current_user(token)["aud"] == "authenticated"


def test_hs256_sin_secreto_configurado_es_401():
    _assert_401(jwt.encode(_claims(), SECRETO_HS256, algorithm="HS256"))


def test_confusion_de_algoritmo_es_401(llave_ec, monkeypatch):
    # Ataque clásico: HS256 usando la llave pública (conocida) como secreto.
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRETO_HS256)
    header = {"alg": "HS256", "typ": "JWT", "kid": KID}
    _assert_401(_token_a_mano(header, llave_ec["publica"].encode()))


def test_alg_none_es_401():
    _assert_401(_token_a_mano({"alg": "none", "typ": "JWT"}, None))
