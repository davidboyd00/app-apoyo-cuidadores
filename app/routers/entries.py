from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from supabase import Client

from ..audit import registrar
from ..deps import get_current_user, get_db
from ..schemas import Entry, EntryCreate, EntryUpdate

router = APIRouter(prefix="/patients/{patient_id}/entries", tags=["entries"])


@router.get("", response_model=list[Entry])
def listar(
    patient_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    desde: datetime | None = None,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    q = (
        db.table("log_entries")
        .select("*")
        .eq("patient_id", str(patient_id))
        .order("occurred_at", desc=True)
        .limit(limit)
    )
    if desde:
        q = q.gte("occurred_at", desde.isoformat())
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
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    # author_id lo pone la base con default auth.uid() (ver db/schema.sql).
    row = payload.model_dump(mode="json") | {"patient_id": str(patient_id)}
    res = db.table("log_entries").insert(row).execute()
    return res.data[0]


@router.patch("/{entry_id}", response_model=Entry)
def actualizar(
    patient_id: UUID,
    entry_id: UUID,
    payload: EntryUpdate,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    cambios = payload.model_dump(mode="json", exclude_unset=True)
    if not cambios:
        raise HTTPException(400, "No hay campos que actualizar")
    res = (
        db.table("log_entries")
        .update(cambios)
        .eq("id", str(entry_id))
        .eq("patient_id", str(patient_id))
        .execute()
    )
    if not res.data:
        # Filtro y RLS colapsan aquí: puede ser "no existe" o "sin permisos".
        raise HTTPException(404, "Entrada no encontrada o sin permisos para editarla")
    registrar(
        db,
        accion="update_entry",
        recurso_tipo="log_entries",
        recurso_id=entry_id,
        patient_id=patient_id,
        metadata={"campos": sorted(cambios.keys())},
    )
    return res.data[0]


@router.delete("/{entry_id}", status_code=204, response_class=Response)
def eliminar(
    patient_id: UUID,
    entry_id: UUID,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    res = (
        db.table("log_entries")
        .delete()
        .eq("id", str(entry_id))
        .eq("patient_id", str(patient_id))
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Entrada no encontrada o sin permisos para eliminarla")
    registrar(
        db,
        accion="delete_entry",
        recurso_tipo="log_entries",
        recurso_id=entry_id,
        patient_id=patient_id,
    )
    return Response(status_code=204)
