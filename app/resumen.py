"""Generación del resumen médico (núcleo, sin HTTP).

Se usa desde el endpoint (en segundo plano) y desde el cron diario. No depende
de FastAPI: así se prueba aislado y se reutiliza en otro proceso.

El contenido final = texto de la IA + nota de trazabilidad armada por el
código. La IA nunca escribe la nota.

Medición: cada etapa (consulta, LLM, nota) deja un log con su duración en ms.
Los logs NUNCA llevan contenido clínico: solo nombre de etapa, tiempo y conteos.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from supabase import Client

from .config import settings
from .llm import ask_llm

log = logging.getLogger("resumen")

PROMPT_RESUMEN = (
    "Genera un resumen médico breve (máx. 8 líneas) apto para consulta médica, "
    "priorizando cambios de síntomas, adherencia a medicamentos y eventos "
    "relevantes. Basa el resumen EXCLUSIVAMENTE en las entradas provistas. "
    "Las entradas marcadas [corregida] fueron corregidas por la familia: usa "
    "su versión vigente, que es la que se te entrega. "
    "Escribe en texto plano, sin Markdown: nada de asteriscos, almohadillas ni "
    "negritas; para listas usa un guion al inicio de la línea."
)

_MARCAS = re.compile(r"\*\*|__")
_TITULO = re.compile(r"^\s{0,3}#{1,6}\s*")
_VINETA = re.compile(r"^(\s*)[*•]\s+")


def texto_plano(texto: str) -> str:
    """Quita el Markdown que el modelo a veces agrega igual: el resumen se
    muestra, se copia y se imprime como texto plano."""
    lineas = []
    for linea in texto.splitlines():
        linea = _TITULO.sub("", _MARCAS.sub("", linea))
        lineas.append(_VINETA.sub(r"\1- ", linea).rstrip())
    return "\n".join(lineas).strip()


# Máximo de correcciones listadas una a una en la nota (el resto se resume).
MAX_CORRECCIONES_LISTADAS = 15

try:
    _ZONA = ZoneInfo("America/Santiago")
except ZoneInfoNotFoundError:  # sin tzdata en el sistema: se informa en UTC
    _ZONA = UTC


@contextmanager
def medir(etapa: str) -> Iterator[None]:
    """Registra la duración de una etapa. Solo tiempos, sin contenido."""
    inicio = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - inicio) * 1000
        log.info("resumen_tiempo etapa=%s ms=%.1f", etapa, ms)


def _fecha(valor: str | datetime) -> str:
    dt = datetime.fromisoformat(valor) if isinstance(valor, str) else valor
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_ZONA).strftime("%d-%m-%Y %H:%M")


# La base guarda en UTC: al LLM se le pasa la hora de Chile, la que vivió la
# familia. Si recibe UTC, informa como del día siguiente lo ocurrido después
# de las 21:00 (encontrado en la grabación de la demo E3).
fecha_local = _fecha


def _armar_contexto(
    entradas: list[dict], max_entradas: int, max_caracteres: int
) -> tuple[str, int]:
    """Toma las entradas más recientes hasta los límites. Devuelve (texto, enviadas).

    `entradas` llega ordenada de la más reciente a la más antigua.
    """
    lineas: list[str] = []
    usados = 0
    for e in entradas[:max_entradas]:
        marca = " [corregida]" if e.get("editada") else ""
        linea = f"{fecha_local(e['occurred_at'])} · {e['kind']}{marca}: {e['content']}"
        if usados + len(linea) + 1 > max_caracteres:
            break
        lineas.append(linea)
        usados += len(linea) + 1
    return "\n".join(lineas), len(lineas)


def _nota_trazabilidad(
    db: Client,
    desde: datetime,
    hasta: datetime,
    vigentes: list[dict],
    anuladas: int,
    enviadas: int,
) -> str:
    """Nota determinista (la arma el código). Todos los datos de corrección
    vienen de la vista `log_entries_vigentes`: no hay consultas extra, salvo
    la de nombres de quienes corrigieron.

    Minimización (Ley 21.719): no repite contenido clínico ni el motivo de las
    correcciones; esos detalles quedan en el historial dentro de la app.
    """
    editadas = [e for e in vigentes if e.get("editada")]

    lineas = [
        "---",
        "Trazabilidad del registro (generada por el sistema, no por IA)",
        f"Período: {_fecha(desde)} a {_fecha(hasta)}",
        (
            f"Entradas consideradas: {len(vigentes)} · Corregidas: {len(editadas)} · "
            f"Anuladas y excluidas del resumen: {anuladas}"
        ),
    ]
    if enviadas < len(vigentes):
        lineas.append(
            f"El texto del resumen se generó con las {enviadas} entradas más recientes "
            f"de {len(vigentes)} por límite de tamaño."
        )

    if not editadas and not anuladas:
        lineas.append("No hubo correcciones ni anulaciones en el período.")
        return "\n".join(lineas)

    if editadas:
        autores = list(
            {e["ultima_revision_autor_id"] for e in editadas if e.get("ultima_revision_autor_id")}
        )
        perfiles = (
            db.table("profiles").select("id, nombre").in_("id", autores).execute().data
            if autores
            else []
        )
        nombres = {p["id"]: p["nombre"] for p in perfiles if p.get("nombre")}

        lineas.append("Correcciones:")
        for e in editadas[:MAX_CORRECCIONES_LISTADAS]:
            quien = nombres.get(e.get("ultima_revision_autor_id"), "un miembro del grupo")
            veces = e.get("revisiones_count") or 1
            extra = f" ({veces} correcciones)" if veces > 1 else ""
            lineas.append(
                f"- Entrada del {_fecha(e['occurred_at'])} ({e['kind']}): "
                f"corregida el {_fecha(e['ultima_revision_at'])} por {quien}{extra}."
            )
        if len(editadas) > MAX_CORRECCIONES_LISTADAS:
            lineas.append(f"- … y {len(editadas) - MAX_CORRECCIONES_LISTADAS} más.")

    lineas.append(
        "La versión original, el autor, la hora y el motivo de cada corrección "
        "o anulación están disponibles en el historial de la aplicación."
    )
    return "\n".join(lineas)


async def construir_resumen(db: Client, patient_id: UUID, desde: datetime, hasta: datetime) -> str:
    """Devuelve el contenido completo del resumen para el período.

    Una sola consulta a la vista trae entradas vigentes, anuladas y los datos
    de corrección. El LLM recibe solo las más recientes dentro de los límites.
    """
    with medir("consulta"):
        filas = (
            db.table("log_entries_vigentes")
            .select(
                "id, occurred_at, kind, content, editada, anulada, "
                "revisiones_count, ultima_revision_at, ultima_revision_autor_id"
            )
            .eq("patient_id", str(patient_id))
            .gte("occurred_at", desde.isoformat())
            .lte("occurred_at", hasta.isoformat())
            .order("occurred_at", desc=True)
            .execute()
            .data
        )
    vigentes = [f for f in filas if not f["anulada"]]
    anuladas = len(filas) - len(vigentes)

    contexto, enviadas = _armar_contexto(
        vigentes, settings.resumen_max_entradas, settings.resumen_max_caracteres
    )
    with medir("llm"):
        texto_ia = texto_plano(
            await ask_llm(contexto, PROMPT_RESUMEN, max_tokens=settings.llm_max_tokens_resumen)
        )

    with medir("nota"):
        nota = _nota_trazabilidad(db, desde, hasta, vigentes, anuladas, enviadas)
    return f"{texto_ia}\n\n{nota}"
