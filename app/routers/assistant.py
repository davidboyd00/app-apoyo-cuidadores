from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from supabase import Client

from ..audit import registrar
from ..deps import get_current_user, get_db
from ..llm import ask_llm
from ..schemas import (
    AssistantAnswer,
    AssistantMessage,
    AssistantQuery,
    ConversationSummary,
)

router = APIRouter(prefix="/patients/{patient_id}/assistant", tags=["assistant"])


def _armar_contexto(entradas: list[dict]) -> str:
    lineas = []
    for e in entradas:
        lineas.append(f"[{e['id']}] {e['occurred_at']} · {e['kind']}: {e['content']}")
    return "\n".join(lineas) if lineas else "(sin entradas en la ventana solicitada)"


@router.post("/ask", response_model=AssistantAnswer)
async def preguntar(
    patient_id: UUID,
    payload: AssistantQuery,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    desde = datetime.now(UTC) - timedelta(days=payload.ventana_dias)
    entradas = (
        db.table("log_entries")
        .select("id, occurred_at, kind, content")
        .eq("patient_id", str(patient_id))
        .gte("occurred_at", desde.isoformat())
        .order("occurred_at", desc=True)
        .limit(200)
        .execute()
        .data
    )
    contexto = _armar_contexto(entradas)
    respuesta = await ask_llm(contexto, payload.pregunta)
    cited = [e["id"] for e in entradas]
    # Continúa conversación existente o inicia una nueva. La API elige el id
    # cuando no viene (en vez de dejar el default de la BD) para poder
    # devolverlo al cliente sin un roundtrip extra.
    conv_id = payload.conversation_id or uuid4()
    # Persistir la respuesta con sus cited_entry_ids (KPI de trazabilidad).
    ins = (
        db.table("assistant_messages")
        .insert(
            {
                "patient_id": str(patient_id),
                "conversation_id": str(conv_id),
                "pregunta": payload.pregunta,
                "respuesta": respuesta,
                "cited_entry_ids": cited,
            }
        )
        .execute()
    )
    # Ley 21.719 · auditoría: qué respuesta se generó y con cuántas entradas
    # (sin contenido: solo id y counts).
    registrar(
        db,
        accion="assistant_query",
        recurso_tipo="assistant_messages",
        recurso_id=ins.data[0]["id"],
        patient_id=patient_id,
        metadata={
            "cited_count": len(cited),
            "ventana_dias": payload.ventana_dias,
            "conversation_id": str(conv_id),
        },
    )
    return AssistantAnswer(respuesta=respuesta, cited_entry_ids=cited, conversation_id=conv_id)


@router.get("/messages", response_model=list[AssistantMessage])
def historial(
    patient_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    antes_de: datetime | None = Query(
        default=None,
        description="Paginación por cursor: devuelve mensajes con created_at < antes_de",
    ),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Historial del asistente para el paciente. Cada mensaje incluye sus
    `cited_entry_ids` — auditar la trazabilidad del KPI (>90% verificable)
    es una consulta directa sobre este endpoint.

    RLS filtra por membresía del grupo, así que un cuidador solo ve el
    historial de sus pacientes; no hay que chequear en código.
    """
    q = (
        db.table("assistant_messages")
        .select("*")
        .eq("patient_id", str(patient_id))
        .order("created_at", desc=True)
        .limit(limit)
    )
    if antes_de:
        q = q.lt("created_at", antes_de.isoformat())
    return q.execute().data


@router.get("/messages/{message_id}", response_model=AssistantMessage)
def obtener_mensaje(
    patient_id: UUID,
    message_id: UUID,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    res = (
        db.table("assistant_messages")
        .select("*")
        .eq("id", str(message_id))
        .eq("patient_id", str(patient_id))
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Mensaje no encontrado o sin permisos para verlo")
    return res.data[0]


@router.get("/conversations", response_model=list[ConversationSummary])
def listar_conversaciones(
    patient_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Agrupa los mensajes por `conversation_id`. La agregación se hace en
    Python (~decenas/cientos de mensajes por paciente en el peor caso del
    piloto — no justifica una vista SQL todavía). RLS filtra automáticamente.
    """
    filas = (
        db.table("assistant_messages")
        .select("conversation_id, pregunta, created_at")
        .eq("patient_id", str(patient_id))
        .order("created_at", desc=False)
        .execute()
        .data
    )
    conversaciones: dict[str, ConversationSummary] = {}
    for f in filas:
        cid = f["conversation_id"]
        c = conversaciones.get(cid)
        if c is None:
            conversaciones[cid] = ConversationSummary(
                conversation_id=cid,
                primera_pregunta=f["pregunta"],
                inicio=f["created_at"],
                ultimo=f["created_at"],
                total_mensajes=1,
            )
        else:
            c.ultimo = f["created_at"]
            c.total_mensajes += 1
    return sorted(conversaciones.values(), key=lambda c: c.ultimo, reverse=True)[:limit]


@router.get("/conversations/{conversation_id}/messages", response_model=list[AssistantMessage])
def mensajes_de_conversacion(
    patient_id: UUID,
    conversation_id: UUID,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Mensajes de una conversación en orden cronológico ascendente (natural
    de lectura). Cada mensaje trae sus `cited_entry_ids` para trazabilidad.
    """
    res = (
        db.table("assistant_messages")
        .select("*")
        .eq("patient_id", str(patient_id))
        .eq("conversation_id", str(conversation_id))
        .order("created_at", desc=False)
        .execute()
        .data
    )
    if not res:
        raise HTTPException(404, "Conversación no encontrada o sin permisos para verla")
    return res
