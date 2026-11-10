from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from supabase import Client

from ..deps import get_current_user, get_db
from ..llm import ask_llm
from ..schemas import Summary

router = APIRouter(prefix="/patients/{patient_id}/summaries", tags=["summaries"])


PROMPT_RESUMEN = (
    "Genera un resumen médico breve (máx. 8 líneas) apto para consulta médica, "
    "priorizando cambios de síntomas, adherencia a medicamentos y eventos "
    "relevantes. Basa el resumen EXCLUSIVAMENTE en las entradas provistas. "
    "Las entradas marcadas [corregida] fueron corregidas por la familia: usa "
    "su versión vigente, que es la que se te entrega."
)

# Máximo de correcciones listadas una a una en la nota (el resto se resume).
MAX_CORRECCIONES_LISTADAS = 15

try:
    _ZONA = ZoneInfo("America/Santiago")
except ZoneInfoNotFoundError:  # sin tzdata en el sistema: se informa en UTC
    _ZONA = UTC


def _fecha(valor: str | datetime) -> str:
    dt = datetime.fromisoformat(valor) if isinstance(valor, str) else valor
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_ZONA).strftime("%d-%m-%Y %H:%M")


def _nota_trazabilidad(
    db: Client,
    patient_id: UUID,
    desde: datetime,
    hasta: datetime,
    entradas: list[dict],
) -> str:
    """Nota determinista (la arma el código, no el LLM) que da validez
    probatoria al resumen: cuántas entradas se consideraron, cuáles fueron
    corregidas (cuándo y por quién) y cuántas se anularon y quedaron fuera.

    Minimización (Ley 21.719): no repite contenido clínico ni el motivo de las
    correcciones; esos detalles quedan en el historial dentro de la app.
    """
    anuladas = (
        db.table("log_entries_vigentes")
        .select("id")
        .eq("patient_id", str(patient_id))
        .eq("anulada", True)
        .gte("occurred_at", desde.isoformat())
        .lte("occurred_at", hasta.isoformat())
        .execute()
        .data
    )
    editadas = [e for e in entradas if e.get("editada")]

    lineas = [
        "---",
        "Trazabilidad del registro (generada por el sistema, no por IA)",
        f"Período: {_fecha(desde)} a {_fecha(hasta)}",
        (
            f"Entradas consideradas: {len(entradas)} · Corregidas: {len(editadas)} · "
            f"Anuladas y excluidas del resumen: {len(anuladas)}"
        ),
    ]

    if not editadas and not anuladas:
        lineas.append("No hubo correcciones ni anulaciones en el período.")
        return "\n".join(lineas)

    if editadas:
        revisiones = (
            db.table("log_entry_revisions")
            .select("entry_id, author_id, created_at")
            .in_("entry_id", [e["id"] for e in editadas])
            .eq("accion", "editar")
            .order("created_at", desc=True)
            .execute()
            .data
        )
        # Última corrección y número de correcciones por entrada.
        ultima: dict[str, dict] = {}
        conteo: dict[str, int] = {}
        for r in revisiones:
            ultima.setdefault(r["entry_id"], r)
            conteo[r["entry_id"]] = conteo.get(r["entry_id"], 0) + 1

        autores = list({r["author_id"] for r in ultima.values()})
        perfiles = (
            db.table("profiles").select("id, nombre").in_("id", autores).execute().data
            if autores
            else []
        )
        nombres = {p["id"]: p["nombre"] for p in perfiles if p.get("nombre")}

        lineas.append("Correcciones:")
        for e in editadas[:MAX_CORRECCIONES_LISTADAS]:
            r = ultima.get(e["id"])
            if r is None:
                continue
            quien = nombres.get(r["author_id"], "un miembro del grupo")
            veces = conteo[e["id"]]
            extra = f" ({veces} correcciones)" if veces > 1 else ""
            lineas.append(
                f"- Entrada del {_fecha(e['occurred_at'])} ({e['kind']}): "
                f"corregida el {_fecha(r['created_at'])} por {quien}{extra}."
            )
        if len(editadas) > MAX_CORRECCIONES_LISTADAS:
            lineas.append(f"- … y {len(editadas) - MAX_CORRECCIONES_LISTADAS} más.")

    lineas.append(
        "La versión original, el autor, la hora y el motivo de cada corrección "
        "o anulación están disponibles en el historial de la aplicación."
    )
    return "\n".join(lineas)


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
    # Solo entradas vigentes: una anulada (registrada por error) no entra al
    # resumen que lee el médico tratante.
    entradas = (
        db.table("log_entries_vigentes")
        .select("id, occurred_at, kind, content, editada")
        .eq("patient_id", str(patient_id))
        .eq("anulada", False)
        .gte("occurred_at", desde.isoformat())
        .lte("occurred_at", hasta.isoformat())
        .order("occurred_at", desc=True)
        .execute()
        .data
    )
    contexto = "\n".join(
        f"{e['occurred_at']} · {e['kind']}{' [corregida]' if e.get('editada') else ''}: "
        f"{e['content']}"
        for e in entradas
    )
    texto_ia = await ask_llm(contexto, PROMPT_RESUMEN)
    contenido = f"{texto_ia}\n\n{_nota_trazabilidad(db, patient_id, desde, hasta, entradas)}"
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
