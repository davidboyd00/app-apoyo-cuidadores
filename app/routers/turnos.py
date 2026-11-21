"""Turnos de cuidado: quién está a cargo y cuándo (relevo familiar, EDT 2.2.2).

No guardan contenido clínico. El "resumen del turno" lo arma el cliente con
las entradas de la bitácora registradas dentro de la ventana del turno.

Autorización 100% en RLS (`db/schema.sql`, tabla `care_shifts`):
- todos los miembros del grupo ven los turnos;
- un admin agenda a cualquier admin o cuidador del grupo;
- un cuidador solo se agenda a sí mismo; un viewer no agenda;
- borra quien creó el turno, la persona a cargo o un admin.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from supabase import Client

from ..deps import get_current_user, get_db
from ..schemas import Turno, TurnoCreate

router = APIRouter(prefix="/patients/{patient_id}/turnos", tags=["turnos"])

VENTANA_MAXIMA = timedelta(days=62)


@router.get("", response_model=list[Turno])
def listar(
    patient_id: UUID,
    desde: datetime | None = Query(default=None, description="ISO 8601; default = hace 1 día"),
    hasta: datetime | None = Query(default=None, description="ISO 8601; default = en 14 días"),
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Turnos que se cruzan con la ventana [desde, hasta], por hora de inicio."""
    ahora = datetime.now(UTC)
    desde = desde or (ahora - timedelta(days=1))
    hasta = hasta or (ahora + timedelta(days=14))
    if desde >= hasta:
        raise HTTPException(400, "El parámetro `desde` debe ser anterior a `hasta`")
    if hasta - desde > VENTANA_MAXIMA:
        raise HTTPException(400, "La ventana máxima es de 62 días; acota `desde` y `hasta`")
    return (
        db.table("care_shifts")
        .select("*")
        .eq("patient_id", str(patient_id))
        .lt("inicio", hasta.isoformat())
        .gt("fin", desde.isoformat())
        .order("inicio")
        .execute()
        .data
    )


@router.post("", response_model=Turno, status_code=201)
def crear(
    patient_id: UUID,
    payload: TurnoCreate,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    """Agenda un turno. Sin `cuidador_id`, el turno es de quien lo crea."""
    fila = {
        "patient_id": str(patient_id),
        "cuidador_id": str(payload.cuidador_id or user["sub"]),
        "inicio": payload.inicio.isoformat(),
        "fin": payload.fin.isoformat(),
    }
    return db.table("care_shifts").insert(fila).execute().data[0]


@router.delete("/{turno_id}", status_code=204, response_class=Response)
def eliminar(
    patient_id: UUID,
    turno_id: UUID,
    db: Client = Depends(get_db),
    user=Depends(get_current_user),
):
    res = (
        db.table("care_shifts")
        .delete()
        .eq("id", str(turno_id))
        .eq("patient_id", str(patient_id))
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Turno no encontrado o sin permisos para eliminarlo")
    return Response(status_code=204)
