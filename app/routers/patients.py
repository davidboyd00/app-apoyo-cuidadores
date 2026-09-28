from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from supabase import Client

from ..audit import registrar
from ..deps import get_current_user, get_db
from ..schemas import Patient, PatientCreate

router = APIRouter(prefix="/patients", tags=["patients"])


@router.get("", response_model=list[Patient])
def listar(db: Client = Depends(get_db), user=Depends(get_current_user)):
    # RLS filtra automáticamente a los pacientes del usuario.
    res = db.table("patients").select("*").execute()
    return res.data


@router.post("", response_model=Patient, status_code=201)
def crear(payload: PatientCreate, db: Client = Depends(get_db), user=Depends(get_current_user)):
    res = db.table("patients").insert(payload.model_dump(mode="json")).execute()
    return res.data[0]


@router.get("/{patient_id}", response_model=Patient)
def obtener(patient_id: UUID, db: Client = Depends(get_db), user=Depends(get_current_user)):
    res = db.table("patients").select("*").eq("id", str(patient_id)).limit(1).execute()
    if not res.data:
        raise HTTPException(404, "Paciente no encontrado o sin acceso")
    return res.data[0]


@router.delete("/{patient_id}", status_code=204, response_class=Response)
def eliminar(
    patient_id: UUID,
    confirmar: bool = Query(False, description="Debe ser true para ejecutar"),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Ley 21.719: derecho de supresión sobre el paciente (ejercido por el
    admin del grupo). FK con `on delete cascade` en db/schema.sql borra
    bitácora, medicamentos, resúmenes, mensajes IA y membresías. RLS
    `patients_delete` filtra a solo admins del grupo — si el usuario no lo es,
    la consulta afecta 0 filas y devuelve 404.
    """
    if not confirmar:
        raise HTTPException(
            400,
            "Debes confirmar el borrado del paciente con ?confirmar=true. "
            "Esta operación elimina TODO su historial y es irreversible.",
        )
    # Auditamos ANTES del delete: la cascada borra al patient_id, pero un
    # audit_log con patient_id "colgante" es válido — la fila existió.
    registrar(
        db,
        accion="delete_patient",
        recurso_tipo="patients",
        recurso_id=patient_id,
        patient_id=patient_id,
    )
    res = db.table("patients").delete().eq("id", str(patient_id)).execute()
    if not res.data:
        raise HTTPException(404, "Paciente no encontrado o no eres admin del grupo")
    return Response(status_code=204)
