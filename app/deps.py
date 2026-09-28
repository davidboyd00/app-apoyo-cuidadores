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


def get_current_user(token: str = Depends(_bearer_token)) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
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
