"""Ley 21.719 · Derechos ARCO del titular sobre sus propios datos.

- GET  /me/data-export : Acceso y portabilidad. Todo lo que el sistema tiene
                        del usuario autenticado.
- DELETE /me           : Supresión de cuenta. Sus entradas de bitácora NO se
                        borran (historial clínico del paciente, base de
                        licitud: interés legítimo del grupo de cuidado) pero
                        su author_id se reasigna al usuario placeholder
                        "eliminado" → no se puede volver a trazar al titular.

Rectificación: se realiza vía los PATCH ya existentes de entradas y
medicamentos. Cambios de email/password los gestiona Supabase Auth
directamente desde el cliente (no pasa por esta API).
"""

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from supabase import Client

from ..audit import registrar
from ..config import settings
from ..deps import get_current_user, get_db
from ..schemas import DataExport, Profile, ProfileUpdate

_POLICY_TEXT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "POLITICA_PRIVACIDAD.md"
)

router = APIRouter(prefix="/me", tags=["me"])


class ConsentStatus(BaseModel):
    version_vigente: str
    texto: str
    aceptada_en: datetime | None


class ConsentAccept(BaseModel):
    version: str


@router.get("/consent", response_model=ConsentStatus)
def consent_estado(db: Client = Depends(get_db), user=Depends(get_current_user)):
    """Devuelve la versión vigente de la política, su texto y el timestamp
    de aceptación del usuario (o null si aún no la aceptó)."""
    aceptada = (
        db.table("consents")
        .select("aceptado_en")
        .eq("user_id", user["sub"])
        .eq("version", settings.policy_version)
        .limit(1)
        .execute()
        .data
    )
    return ConsentStatus(
        version_vigente=settings.policy_version,
        texto=_POLICY_TEXT_PATH.read_text(encoding="utf-8"),
        aceptada_en=aceptada[0]["aceptado_en"] if aceptada else None,
    )


@router.post("/consent", status_code=201, response_model=ConsentStatus)
def consent_aceptar(
    payload: ConsentAccept,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Registra la aceptación. La versión enviada debe coincidir con la
    vigente — evita "aceptar" una versión antigua o inventada. Idempotente:
    reintentar no crea duplicados (PK compuesta user_id+version)."""
    if payload.version != settings.policy_version:
        raise HTTPException(
            409,
            f"Solo se puede aceptar la versión vigente ({settings.policy_version}). "
            f"Recibimos: {payload.version}.",
        )
    db.table("consents").upsert(
        {"user_id": user["sub"], "version": payload.version},
        on_conflict="user_id,version",
    ).execute()
    registrar(
        db,
        accion="consent_accept",
        recurso_tipo="consents",
        metadata={"version": payload.version},
    )
    return consent_estado(db=db, user=user)


@router.get("/profile", response_model=Profile)
def perfil_actual(db: Client = Depends(get_db), user=Depends(get_current_user)):
    """Devuelve el perfil del usuario autenticado. Si aún no existe, lo crea
    vacío y lo devuelve — así el cliente siempre recibe la misma forma."""
    uid = user["sub"]
    res = (
        db.table("profiles")
        .select("*")
        .eq("id", uid)
        .limit(1)
        .execute()
        .data
    )
    if res:
        return res[0]
    creado = db.table("profiles").insert({"id": uid}).execute().data[0]
    return creado


@router.put("/profile", response_model=Profile)
def actualizar_perfil(
    payload: ProfileUpdate,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    uid = user["sub"]
    datos = payload.model_dump(mode="json", exclude_unset=True)
    res = (
        db.table("profiles")
        .upsert({"id": uid, **datos, "updated_at": datetime.now(UTC).isoformat()})
        .execute()
    )
    return res.data[0]


@router.get("/data-export", response_model=DataExport)
def data_export(db: Client = Depends(get_db), user=Depends(get_current_user)):
    uid = user["sub"]

    # RLS filtra automáticamente: solo veo lo que puedo ver.
    membresias = (
        db.table("care_members")
        .select("patient_id, role, created_at")
        .eq("user_id", uid)
        .execute()
        .data
    )
    entradas = (
        db.table("log_entries")
        .select("*")
        .eq("author_id", uid)
        .order("occurred_at", desc=True)
        .execute()
        .data
    )
    mensajes = (
        db.table("assistant_messages")
        .select("*")
        .eq("author_id", uid)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    pacientes = db.table("patients").select("*").eq("created_by", uid).execute().data

    registrar(
        db,
        accion="self_data_export",
        recurso_tipo="user",
        recurso_id=uid,
        metadata={
            "entradas": len(entradas),
            "mensajes": len(mensajes),
            "pacientes_creados": len(pacientes),
        },
    )
    return DataExport(
        exportado_en=datetime.now(UTC),
        usuario={"id": uid, "email": user.get("email")},
        membresias=membresias,
        entradas_bitacora=entradas,
        mensajes_asistente=mensajes,
        pacientes_creados=pacientes,
    )


@router.delete("", status_code=204, response_class=Response)
def eliminar_cuenta(
    confirmar: bool = Query(False, description="Debe ser true para ejecutar"),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Ley 21.719 art. 8 (derecho de supresión / cancelación).

    Efectos:
      - Autoría de bitácora y de mensajes IA → usuario "eliminado".
      - Membresías del usuario borradas.
      - Pacientes de los que era único miembro → eliminados en cascada
        (nadie más puede acceder ni ejercer derechos sobre ellos).
      - Cuenta en auth.users eliminada.

    La operación es irreversible.
    """
    if not confirmar:
        raise HTTPException(
            400,
            "Debes confirmar el borrado con ?confirmar=true. Esta operación es irreversible.",
        )
    # Auditamos ANTES de borrar para que la fila persista con user_id no-nulo
    # (después el on delete set null cambia user_id a NULL, pero la acción
    # queda registrada con su timestamp).
    registrar(
        db,
        accion="self_delete",
        recurso_tipo="user",
        recurso_id=user["sub"],
    )
    # SECURITY DEFINER function en la BD (ver db/schema.sql). La API no toca
    # auth.users ni reasigna author_id directamente — sería violar RLS.
    db.rpc("delete_my_account").execute()
    return Response(status_code=204)
