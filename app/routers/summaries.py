from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends
from supabase import Client

from ..deps import get_current_user, get_db
from ..llm import ask_llm
from ..schemas import Summary

router = APIRouter(prefix="/patients/{patient_id}/summaries", tags=["summaries"])


PROMPT_RESUMEN = (
    "Genera un resumen médico breve (máx. 8 líneas) apto para consulta médica, "
    "priorizando cambios de síntomas, adherencia a medicamentos y eventos "
    "relevantes. Basa el resumen EXCLUSIVAMENTE en las entradas provistas."
)


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


@router.post("", response_model=Summary, status_code=201)
async def generar(
    patient_id: UUID,
    dias: int = 30,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    hasta = datetime.now(UTC)
    desde = hasta - timedelta(days=dias)
    entradas = (
        db.table("log_entries")
        .select("occurred_at, kind, content")
        .eq("patient_id", str(patient_id))
        .gte("occurred_at", desde.isoformat())
        .lte("occurred_at", hasta.isoformat())
        .order("occurred_at", desc=True)
        .execute()
        .data
    )
    contexto = "\n".join(f"{e['occurred_at']} · {e['kind']}: {e['content']}" for e in entradas)
    contenido = await ask_llm(contexto, PROMPT_RESUMEN)
    res = (
        db.table("summaries")
        .insert(
            {
                "patient_id": str(patient_id),
                "desde": desde.isoformat(),
                "hasta": hasta.isoformat(),
                "contenido": contenido,
            }
        )
        .execute()
    )
    return res.data[0]
