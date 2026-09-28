"""Consulta del registro de auditoría (Ley 21.719 · Punto 2).

RLS ya restringe la lectura a admins del grupo (policy `audit_read`), así que
este router solo expone la consulta. No hay POST — el registro se escribe
únicamente vía la función `audit_write` de la BD.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from supabase import Client

from ..deps import get_current_user, get_db

router = APIRouter(prefix="/patients/{patient_id}/audit", tags=["audit"])


class AuditEntry(BaseModel):
    id: UUID
    user_id: UUID | None
    accion: str
    recurso_tipo: str
    recurso_id: UUID | None
    patient_id: UUID | None
    metadata: dict
    created_at: datetime


@router.get("", response_model=list[AuditEntry])
def historial(
    patient_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    antes_de: datetime | None = Query(default=None),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Quién ha accedido a los datos del paciente. Transparencia hacia el
    titular del dato (paciente y sus admins). Un caregiver o viewer NO ve
    esto: la policy `audit_read` lo restringe a admins del grupo.
    """
    q = (
        db.table("audit_log")
        .select("*")
        .eq("patient_id", str(patient_id))
        .order("created_at", desc=True)
        .limit(limit)
    )
    if antes_de:
        q = q.lt("created_at", antes_de.isoformat())
    return q.execute().data
