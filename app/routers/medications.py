from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from supabase import Client

from ..deps import get_current_user, get_db
from ..schemas import (
    AdherenceReport,
    Medication,
    MedicationCreate,
    MedicationDose,
    MedicationDoseCreate,
    MedicationUpdate,
    UpcomingDose,
)

router = APIRouter(prefix="/patients/{patient_id}/medications", tags=["medications"])

# Timezone del paciente. MVP: fija (proyecto chileno). Si el proyecto se
# expande a otros husos, migrar a columna `patients.timezone`.
TZ_CHILE = ZoneInfo("America/Santiago")


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


# ---------------------------------------------------------------------------
# Dosis (adherencia + KPI relevo familiar)
# ---------------------------------------------------------------------------


@router.post("/{medication_id}/doses", response_model=MedicationDose, status_code=201)
def registrar_dosis(
    patient_id: UUID,
    medication_id: UUID,
    payload: MedicationDoseCreate,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    # Coherencia status/taken_at (defensa en profundidad: el CHECK de la BD
    # también lo garantiza).
    if payload.status == "tomada" and payload.taken_at is None:
        raise HTTPException(400, "Una dosis 'tomada' requiere `taken_at`.")
    if payload.status == "omitida" and payload.taken_at is not None:
        raise HTTPException(400, "Una dosis 'omitida' no puede tener `taken_at`.")

    # author_id lo pone la base con default auth.uid() (KPI relevo familiar).
    row = payload.model_dump(mode="json") | {
        "patient_id": str(patient_id),
        "medication_id": str(medication_id),
    }
    res = db.table("medication_doses").insert(row).execute()
    return res.data[0]


@router.get("/{medication_id}/doses", response_model=list[MedicationDose])
def listar_dosis(
    patient_id: UUID,
    medication_id: UUID,
    desde: datetime | None = Query(None),
    hasta: datetime | None = Query(None),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    q = (
        db.table("medication_doses")
        .select("*")
        .eq("patient_id", str(patient_id))
        .eq("medication_id", str(medication_id))
        .order("scheduled_for", desc=True)
    )
    if desde is not None:
        q = q.gte("scheduled_for", desde.isoformat())
    if hasta is not None:
        q = q.lte("scheduled_for", hasta.isoformat())
    return q.execute().data


def proyectar_upcoming(meds: list[dict], ahora: datetime, within_hours: int) -> list[dict]:
    """Proyecta próximas dosis desde `medications.horarios` en la ventana pedida.

    Función pura (sin DB) para poder testearla con horas controladas.
    `ahora` debe ser timezone-aware; se combina con cada hora de `horarios`
    para producir un `datetime` en la misma tz.
    """
    ventana = ahora + timedelta(hours=within_hours)
    proximas: list[dict] = []
    for m in meds:
        for h_raw in m.get("horarios") or []:
            hora = h_raw if isinstance(h_raw, time) else time.fromisoformat(str(h_raw))
            candidato = datetime.combine(ahora.date(), hora, tzinfo=ahora.tzinfo)
            while candidato < ahora:
                candidato += timedelta(days=1)
            while candidato <= ventana:
                proximas.append(
                    {
                        "medication_id": m["id"],
                        "nombre": m["nombre"],
                        "dosis": m.get("dosis"),
                        "scheduled_for": candidato,
                    }
                )
                candidato += timedelta(days=1)
    proximas.sort(key=lambda x: x["scheduled_for"])
    return proximas


@router.get("/upcoming", response_model=list[UpcomingDose])
def proximas_dosis(
    patient_id: UUID,
    within_hours: int = Query(24, ge=1, le=168),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Proyecta las próximas dosis en la ventana pedida a partir de
    `medications.horarios`. El cliente móvil agenda notificaciones locales
    (expo-notifications) con este resultado.

    Nota: los medicamentos con solo `frecuencia_horas` (sin horarios fijos)
    aún no se proyectan aquí; requieren consultar la última dosis tomada.
    """
    meds = (
        db.table("medications")
        .select("id, nombre, dosis, horarios")
        .eq("patient_id", str(patient_id))
        .eq("activo", True)
        .execute()
        .data
    )
    return proyectar_upcoming(meds, datetime.now(tz=TZ_CHILE), within_hours)


@router.get("/adherence", response_model=AdherenceReport)
def adherencia(
    patient_id: UUID,
    desde: datetime = Query(...),
    hasta: datetime = Query(...),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    if hasta <= desde:
        raise HTTPException(400, "El rango es inválido: `hasta` debe ser posterior a `desde`.")

    doses = (
        db.table("medication_doses")
        .select("status")
        .eq("patient_id", str(patient_id))
        .gte("scheduled_for", desde.isoformat())
        .lte("scheduled_for", hasta.isoformat())
        .execute()
        .data
    )

    total = len(doses)
    tomadas = sum(1 for d in doses if d["status"] == "tomada")
    omitidas = sum(1 for d in doses if d["status"] == "omitida")
    postpuestas = sum(1 for d in doses if d["status"] == "postpuesta")
    pct = round(tomadas / total * 100.0, 1) if total else 0.0

    return {
        "desde": desde,
        "hasta": hasta,
        "total_dosis_registradas": total,
        "tomadas": tomadas,
        "omitidas": omitidas,
        "postpuestas": postpuestas,
        "porcentaje_adherencia": pct,
    }
