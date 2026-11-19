import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from supabase import Client

from ..deps import get_current_user, get_db
from ..resumen import construir_resumen
from ..schemas import Summary

router = APIRouter(prefix="/patients/{patient_id}/summaries", tags=["summaries"])
log = logging.getLogger(__name__)

# Un resumen listo dentro de esta ventana se reutiliza en vez de llamar al LLM.
VENTANA_REUSO = timedelta(hours=24)
# Un resumen 'generando' más viejo que esto se considera abandonado (la tarea
# murió con el proceso) y no bloquea uno nuevo.
VENTANA_GENERANDO = timedelta(minutes=10)


def _reusable(db: Client, patient_id: UUID, dias: int, ahora: datetime) -> dict | None:
    """Resumen listo reciente con la misma ventana, o en curso hace poco."""
    candidatos = (
        db.table("summaries")
        .select("*")
        .eq("patient_id", str(patient_id))
        .gte("created_at", (ahora - VENTANA_REUSO).isoformat())
        .order("created_at", desc=True)
        .limit(10)
        .execute()
        .data
    )
    for s in candidatos:
        desde = datetime.fromisoformat(s["desde"])
        hasta = datetime.fromisoformat(s["hasta"])
        misma_ventana = abs((hasta - desde).total_seconds() - dias * 86400) < 60
        if not misma_ventana:
            continue
        if s["estado"] == "listo":
            return s
        if (
            s["estado"] == "generando"
            and datetime.fromisoformat(s["created_at"]) >= ahora - VENTANA_GENERANDO
        ):
            return s
    return None


async def _completar(
    db: Client, summary_id: str, patient_id: UUID, desde: datetime, hasta: datetime
) -> None:
    """Corre después de responder 202. Nunca registra contenido clínico: solo
    el tipo de error, para diagnóstico."""
    try:
        contenido = await construir_resumen(db, patient_id, desde, hasta)
        db.table("summaries").update({"contenido": contenido, "estado": "listo"}).eq(
            "id", summary_id
        ).execute()
    except Exception as exc:  # noqa: BLE001 - cualquier fallo debe quedar marcado, no perderse
        log.error("resumen %s falló: %s", summary_id, type(exc).__name__)
        db.table("summaries").update({"estado": "error"}).eq("id", summary_id).execute()


@router.get("", response_model=list[Summary])
def listar(patient_id: UUID, db: Client = Depends(get_db), user=Depends(get_current_user)):
    return (
        db.table("summaries")
        .select("*")
        .eq("patient_id", str(patient_id))
        .order("created_at", desc=True)
        .execute()
        .data
    )


@router.get("/{summary_id}", response_model=Summary)
def obtener(
    patient_id: UUID,
    summary_id: UUID,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Estado de un resumen. La app consulta aquí si no usa Realtime."""
    res = (
        db.table("summaries")
        .select("*")
        .eq("id", str(summary_id))
        .eq("patient_id", str(patient_id))
        .limit(1)
        .execute()
        .data
    )
    if not res:
        raise HTTPException(404, "Resumen no encontrado o sin permisos para verlo")
    return res[0]


@router.post("", response_model=Summary, status_code=202)
def generar(
    patient_id: UUID,
    background: BackgroundTasks,
    response: Response,
    dias: int = Query(30, ge=1, le=90),
    forzar: bool = Query(False, description="Ignora un resumen reciente y genera uno nuevo."),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Pide un resumen médico.

    - Si hay un resumen listo de las últimas 24 h con la misma ventana, lo
      devuelve al instante con status 200 (no llama al LLM).
    - Si ya hay uno en curso, devuelve ese con status 200.
    - Si no, crea el resumen en estado `generando` y lo completa en segundo
      plano: responde 202. La app se entera por Realtime o consultando
      `GET /summaries/{id}`.
    """
    ahora = datetime.now(UTC)
    if not forzar:
        existente = _reusable(db, patient_id, dias, ahora)
        if existente is not None:
            response.status_code = 200
            return existente

    desde = ahora - timedelta(days=dias)
    fila = (
        db.table("summaries")
        .insert(
            {
                "patient_id": str(patient_id),
                "desde": desde.isoformat(),
                "hasta": ahora.isoformat(),
                "estado": "generando",
                "contenido": None,
            }
        )
        .execute()
        .data[0]
    )
    background.add_task(_completar, db, fila["id"], patient_id, desde, ahora)
    return fila
