from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from supabase import Client

from ..deps import get_current_user, get_db
from ..schemas import AsistenteMetric, RelevoMetric, VentanaTemporal

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
        db.table("log_entries_vigentes")
        .select("author_id")
        .eq("patient_id", str(patient_id))
        .eq("anulada", False)
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


UMBRAL_VERIFICABLE = 90.0


@router.get("/asistente", response_model=AsistenteMetric)
def asistente_verificable(
    patient_id: UUID,
    desde: datetime | None = Query(default=None, description="ISO 8601; default = hace 30 días"),
    hasta: datetime | None = Query(default=None, description="ISO 8601; default = ahora"),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """KPI del asistente (>90% de respuestas verificables).

    Verificable = cita al menos una entrada de la bitácora, o declara que la
    bitácora no alcanza (`suficiente=false`): en ambos casos la familia puede
    comprobar la respuesta contra lo registrado. Solo lee ids y flags, nunca
    el texto de preguntas ni respuestas. RLS filtra por membresía.
    """
    hasta = hasta or datetime.now(UTC)
    desde = desde or (hasta - timedelta(days=30))
    if desde >= hasta:
        raise HTTPException(400, "El parámetro `desde` debe ser anterior a `hasta`")

    filas = (
        db.table("assistant_messages")
        .select("cited_entry_ids, suficiente")
        .eq("patient_id", str(patient_id))
        .gte("created_at", desde.isoformat())
        .lte("created_at", hasta.isoformat())
        .execute()
        .data
    )
    verificables = sum(
        1 for f in filas if (f.get("cited_entry_ids") or []) or f.get("suficiente") is False
    )
    total = len(filas)
    porcentaje = round(100.0 * verificables / total, 1) if total else 0.0
    return AsistenteMetric(
        ventana=VentanaTemporal(desde=desde, hasta=hasta),
        total_respuestas=total,
        verificables=verificables,
        porcentaje_verificable=porcentaje,
        cumple_kpi=total > 0 and porcentaje >= UMBRAL_VERIFICABLE,
    )
