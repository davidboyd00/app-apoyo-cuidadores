from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from supabase import Client

from ..deps import get_current_user, get_db
from ..schemas import Medication, MedicationCreate, MedicationUpdate

router = APIRouter(prefix="/patients/{patient_id}/medications", tags=["medications"])


@router.get("", response_model=list[Medication])
def listar(
    patient_id: UUID,
    incluir_inactivos: bool = Query(False),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    q = db.table("medications").select("*").eq("patient_id", str(patient_id))
    if not incluir_inactivos:
        q = q.eq("activo", True)
    return q.execute().data


@router.post("", response_model=Medication, status_code=201)
def crear(
    patient_id: UUID,
    payload: MedicationCreate,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    row = payload.model_dump(mode="json") | {"patient_id": str(patient_id)}
    res = db.table("medications").insert(row).execute()
    return res.data[0]


@router.patch("/{medication_id}", response_model=Medication)
def actualizar(
    patient_id: UUID,
    medication_id: UUID,
    payload: MedicationUpdate,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    cambios = payload.model_dump(mode="json", exclude_unset=True)
    if not cambios:
        raise HTTPException(400, "No hay campos que actualizar")
    res = (
        db.table("medications")
        .update(cambios)
        .eq("id", str(medication_id))
        .eq("patient_id", str(patient_id))
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Medicamento no encontrado o sin permisos")
    return res.data[0]


@router.post("/{medication_id}/desactivar", response_model=Medication)
def desactivar(
    patient_id: UUID,
    medication_id: UUID,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    # Desactivar (no borrar): preserva el historial para trazabilidad clínica.
    res = (
        db.table("medications")
        .update({"activo": False})
        .eq("id", str(medication_id))
        .eq("patient_id", str(patient_id))
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Medicamento no encontrado o sin permisos")
    return res.data[0]
