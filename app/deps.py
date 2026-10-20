import json
import time
import urllib.request
from urllib.error import URLError

from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from supabase import Client, create_client

from .config import settings


def _bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta encabezado Authorization: Bearer <token>",
        )
    return authorization.split(" ", 1)[1].strip()


# Supabase firma los JWT de usuario con llaves asimétricas (ES256/RS256) en
# proyectos nuevos; la llave pública se publica en el JWKS del proyecto.
# HS256 (secreto compartido) queda solo para proyectos legacy.
_ALGS_ASIMETRICOS = {"ES256", "RS256"}
_JWKS_TTL_S = 600  # refresco normal de la caché
_JWKS_MIN_REFETCH_S = 60  # ante un kid desconocido (rotación), máx. 1 fetch/min
_jwks_cache: dict = {"keys": [], "fetched_at": float("-inf")}


def _fetch_jwks() -> list[dict]:
    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp).get("keys", [])


def _llave_publica(kid: str | None, alg: str) -> dict:
    """Busca en el JWKS (cacheado) la llave pública con ese `kid`."""

    def buscar() -> dict | None:
        return next((k for k in _jwks_cache["keys"] if k.get("kid") == kid), None)

    edad = time.monotonic() - _jwks_cache["fetched_at"]
    llave = buscar() if edad < _JWKS_TTL_S else None
    # Si no está, refrescamos (puede haber rotado), pero con un mínimo entre
    # fetches para que tokens con kid inventado no martillen a Supabase.
    if llave is None and edad >= _JWKS_MIN_REFETCH_S:
        try:
            _jwks_cache["keys"] = _fetch_jwks()
        except (URLError, TimeoutError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No pudimos verificar tu sesión en este momento. Reintenta en unos segundos.",
            ) from exc
        _jwks_cache["fetched_at"] = time.monotonic()
        llave = buscar()
    if llave is None:
        raise JWTError("kid desconocido")
    if llave.get("alg") and llave["alg"] != alg:
        raise JWTError("alg del token no coincide con la llave")
    return llave


def get_current_user(token: str = Depends(_bearer_token)) -> dict:
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        # Cada rama fija su algoritmo y su tipo de llave: así un token HS256
        # firmado con la llave pública como secreto no puede colarse.
        if alg in _ALGS_ASIMETRICOS:
            llave = _llave_publica(header.get("kid"), alg)
        elif alg == "HS256" and settings.supabase_jwt_secret:
            llave = settings.supabase_jwt_secret
        else:
            raise JWTError(f"algoritmo no aceptado: {alg}")
        payload = jwt.decode(token, llave, algorithms=[alg], audience="authenticated")
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
        ) from exc
    return payload


def get_db(token: str = Depends(_bearer_token)) -> Client:
    # Cliente Supabase autenticado con el JWT del usuario: las políticas RLS
    # aplican en toda consulta. NUNCA usar service role aquí.
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
    client.postgrest.auth(token)
    return client


def require_consent(
    db: Client = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> None:
    """Ley 21.719: bloquea la operación si el usuario no aceptó la versión
    vigente de la política de privacidad. Se aplica como dependencia a los
    routers de negocio; los endpoints ARCO (`/me/*`) están exentos para no
    volver imposible el ejercicio de derechos."""
    res = (
        db.table("consents")
        .select("version")
        .eq("user_id", user["sub"])
        .eq("version", settings.policy_version)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Debes aceptar la versión vigente de la política de privacidad "
                f"({settings.policy_version}) antes de continuar. "
                f"Consulta GET /me/consent y confirma con POST /me/consent."
            ),
        )
