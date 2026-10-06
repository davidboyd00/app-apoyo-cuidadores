"""Red de cuidado: listar, invitar y remover miembros del grupo.

- GET    /patients/{id}/members          miembros con perfil + email
- POST   /patients/{id}/members          invitar por email (solo admin)
- DELETE /patients/{id}/members/{uid}    quitar (solo admin; el trigger de
                                         la BD prevé no quedar sin admin)
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from postgrest.exceptions import APIError
from supabase import Client

from ..audit import registrar
from ..deps import get_current_user, get_db
from ..schemas import CareMember, MemberInvite

router = APIRouter(prefix="/patients/{patient_id}/members", tags=["members"])


@router.get("", response_model=list[CareMember])
def listar(
    patient_id: UUID,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    res = db.rpc(
        "list_care_members", {"p_patient_id": str(patient_id)}
    ).execute()
    return res.data or []


@router.post("", response_model=CareMember, status_code=201)
def invitar(
    patient_id: UUID,
    payload: MemberInvite,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        db.rpc(
            "invite_member_by_email",
            {
                "p_patient_id": str(patient_id),
                "p_email": payload.email,
                "p_role": payload.role,
            },
        ).execute()
    except APIError as exc:
        # P0002: usuario no encontrado por email; 42501: no soy admin.
        code = getattr(exc, "code", "")
        if code == "P0002":
            raise HTTPException(
                404,
                f"No existe una cuenta con el correo {payload.email}. "
                f"Pídele que se registre primero.",
            ) from exc
        if code == "42501":
            raise HTTPException(
                403, "Solo un admin del grupo puede invitar miembros."
            ) from exc
        raise

    registrar(
        db,
        accion="invite_member",
        recurso_tipo="care_members",
        patient_id=patient_id,
        metadata={"role": payload.role},
    )

    # Devolver el miembro recién creado desde el listado.
    miembros = db.rpc(
        "list_care_members", {"p_patient_id": str(patient_id)}
    ).execute().data or []
    for m in miembros:
        if m["email"].lower() == payload.email.lower():
            return m
    raise HTTPException(500, "El miembro se invitó pero no se encontró al listarlo.")


@router.delete("/{user_id}", status_code=204)
def remover(
    patient_id: UUID,
    user_id: UUID,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        res = (
            db.table("care_members")
            .delete()
            .eq("patient_id", str(patient_id))
            .eq("user_id", str(user_id))
            .execute()
        )
    except APIError as exc:
        # P0001 = el trigger del último admin.
        if getattr(exc, "code", "") == "P0001":
            raise HTTPException(
                409,
                "No se puede eliminar al único admin del grupo. "
                "Asigna otro admin antes de retirarte.",
            ) from exc
        raise

    if not res.data:
        raise HTTPException(404, "Miembro no encontrado o sin permisos.")

    registrar(
        db,
        accion="remove_member",
        recurso_tipo="care_members",
        patient_id=patient_id,
        recurso_id=user_id,
    )
    return None
