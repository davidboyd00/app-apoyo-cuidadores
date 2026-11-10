from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from supabase import Client

from ..audit import registrar
from ..deps import get_current_user, get_db
from ..idempotencia import insertar_o_recuperar
from ..schemas import (
    Entry,
    EntryAnulacion,
    EntryCreate,
    EntryKind,
    EntryRevision,
    EntryRevisionEditar,
)

router = APIRouter(prefix="/patients/{patient_id}/entries", tags=["entries"])

# Lectura: estado vigente de cada entrada (la base de la bitácora es insert-only).
VISTA = "log_entries_vigentes"


def _vigente(db: Client, patient_id: UUID, entry_id: UUID) -> dict | None:
    res = (
        db.table(VISTA)
        .select("*")
        .eq("id", str(entry_id))
        .eq("patient_id", str(patient_id))
        .limit(1)
        .execute()
        .data
    )
    return res[0] if res else None


@router.get("", response_model=list[Entry])
def listar(
    patient_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    desde: datetime | None = None,
    kind: EntryKind | None = Query(
        default=None, description="Filtra por tipo de entrada (en el servidor)."
    ),
    antes: datetime | None = Query(
        default=None,
        description=(
            "Cursor de paginación: solo entradas con occurred_at estrictamente "
            "anterior (ISO 8601). Para la página siguiente, enviar el "
            "occurred_at de la última entrada recibida."
        ),
    ),
    incluir_anuladas: bool = Query(
        default=False,
        description="Incluye entradas anuladas (marcadas con `anulada=true`). Por defecto no aparecen.",
    ),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Feed de la bitácora, más reciente primero (occurred_at desc).

    Devuelve el estado vigente de cada entrada: si fue corregida, `content`,
    `kind` y `occurred_at` son los de la última revisión. El historial completo
    está en `GET /entries/{id}/revisiones`.
    """
    q = (
        db.table(VISTA)
        .select("*")
        .eq("patient_id", str(patient_id))
        .order("occurred_at", desc=True)
        .limit(limit)
    )
    if desde:
        q = q.gte("occurred_at", desde.isoformat())
    if kind:
        q = q.eq("kind", kind)
    if antes:
        q = q.lt("occurred_at", antes.isoformat())
    if not incluir_anuladas:
        q = q.eq("anulada", False)
    data = q.execute().data

    # Ley 21.719 · auditoría: registrar solo si vi entradas ajenas.
    ajenas = sum(1 for e in data if e.get("author_id") and e["author_id"] != user["sub"])
    if ajenas:
        registrar(
            db,
            accion="read_entries_ajenas",
            recurso_tipo="log_entries",
            patient_id=patient_id,
            metadata={"total": len(data), "ajenas": ajenas},
        )
    return data


@router.post("", response_model=Entry, status_code=201)
def crear(
    patient_id: UUID,
    payload: EntryCreate,
    response: Response,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Registra una entrada. Es idempotente si viene `client_id`.

    Un reintento con el mismo `client_id` devuelve la entrada ya creada con
    status 200 (no duplica ni falla). Con `registrado_en` se guarda la hora en
    que se escribió sin conexión; `created_at` es la hora de sincronización.
    """
    # author_id lo pone la base con default auth.uid() (ver db/schema.sql).
    row = payload.model_dump(mode="json") | {"patient_id": str(patient_id)}
    fila, creada = insertar_o_recuperar(
        db,
        "log_entries",
        row,
        author_id=user["sub"],
        client_id=payload.client_id,
    )
    if not creada:
        response.status_code = 200
        return _vigente(db, patient_id, fila["id"]) or fila
    return fila | {"anulada": False, "editada": False}


@router.get("/{entry_id}/revisiones", response_model=list[EntryRevision])
def listar_revisiones(
    patient_id: UUID,
    entry_id: UUID,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Historial completo de una entrada: revisiones y anulación, en orden
    cronológico. Lo ven todos los miembros del grupo (RLS)."""
    data = (
        db.table("log_entry_revisions")
        .select("*")
        .eq("entry_id", str(entry_id))
        .eq("patient_id", str(patient_id))
        .order("created_at", desc=False)
        .execute()
        .data
    )
    ajenas = sum(1 for r in data if r["author_id"] != user["sub"])
    if ajenas:
        registrar(
            db,
            accion="read_revisiones_ajenas",
            recurso_tipo="log_entry_revisions",
            recurso_id=entry_id,
            patient_id=patient_id,
            metadata={"total": len(data), "ajenas": ajenas},
        )
    return data


@router.post("/{entry_id}/revisiones", response_model=Entry, status_code=201)
def revisar(
    patient_id: UUID,
    entry_id: UUID,
    payload: EntryRevisionEditar,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Corrige una entrada. No la modifica: agrega una revisión con autor, hora
    y motivo. Solo el autor original o un admin pueden revisar (RLS)."""
    actual = _vigente(db, patient_id, entry_id)
    if actual is None:
        # RLS y filtro colapsan aquí: no existe o no tienes acceso.
        raise HTTPException(404, "Entrada no encontrada o sin permisos para revisarla")
    if actual["anulada"]:
        raise HTTPException(400, "La entrada está anulada y no admite más revisiones")

    db.table("log_entry_revisions").insert(
        payload.model_dump(mode="json")
        | {
            "entry_id": str(entry_id),
            "patient_id": str(patient_id),
            "accion": "editar",
        }
    ).execute()
    registrar(
        db,
        accion="revisar_entrada",
        recurso_tipo="log_entries",
        recurso_id=entry_id,
        patient_id=patient_id,
        metadata={"accion": "editar"},
    )
    return _vigente(db, patient_id, entry_id)


@router.post("/{entry_id}/anulacion", response_model=Entry, status_code=201)
def anular(
    patient_id: UUID,
    entry_id: UUID,
    payload: EntryAnulacion,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Anula una entrada (p. ej. registrada por error). La entrada no se borra:
    queda en el historial marcada como anulada, con motivo, autor y hora.
    La anulación es definitiva."""
    actual = _vigente(db, patient_id, entry_id)
    if actual is None:
        raise HTTPException(404, "Entrada no encontrada o sin permisos para anularla")
    if actual["anulada"]:
        raise HTTPException(400, "La entrada ya está anulada")

    db.table("log_entry_revisions").insert(
        payload.model_dump(mode="json")
        | {
            "entry_id": str(entry_id),
            "patient_id": str(patient_id),
            "accion": "anular",
        }
    ).execute()
    registrar(
        db,
        accion="anular_entrada",
        recurso_tipo="log_entries",
        recurso_id=entry_id,
        patient_id=patient_id,
        metadata={"accion": "anular"},
    )
    return _vigente(db, patient_id, entry_id)
