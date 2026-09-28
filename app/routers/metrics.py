from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from supabase import Client

from ..deps import get_current_user, get_db
from ..schemas import RelevoMetric, VentanaTemporal

router = APIRouter(prefix="/patients/{patient_id}/metrics", tags=["metrics"])


@router.get("/relevo", response_model=RelevoMetric)
def relevo_familiar(
    patient_id: UUID,
    desde: datetime | None = Query(default=None, description="ISO 8601; default = hace 7 días"),
    hasta: datetime | None = Query(default=None, description="ISO 8601; default = ahora"),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """KPI de relevo familiar: cuántos autores distintos registraron entradas
    en la ventana. Se considera "relevo" >= 2 autores distintos por semana
    (>60% de pacientes con relevo es el KPI comprometido).

    RLS filtra automáticamente: si el usuario no pertenece al grupo del
    paciente, la consulta ve 0 filas (mismo resultado que un paciente sin
    actividad, sin filtrar información).
    """
    hasta = hasta or datetime.now(UTC)
    desde = desde or (hasta - timedelta(days=7))
    if desde >= hasta:
        raise HTTPException(400, "El parámetro `desde` debe ser anterior a `hasta`")

    entradas = (
        db.table("log_entries")
        .select("author_id")
        .eq("patient_id", str(patient_id))
        .gte("occurred_at", desde.isoformat())
        .lte("occurred_at", hasta.isoformat())
        .execute()
        .data
    )
    autores = {e["author_id"] for e in entradas if e.get("author_id")}
    return RelevoMetric(
        ventana=VentanaTemporal(desde=desde, hasta=hasta),
        total_entries=len(entradas),
        autores_distintos=len(autores),
        cumple_kpi=len(autores) >= 2,
    )
