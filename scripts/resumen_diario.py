"""Resumen diario por paciente (cron de GitHub Actions).

Genera, una vez al día y por paciente con actividad en las últimas 24 h, un
resumen médico con el mismo núcleo que usa la API (`app/resumen.py`): texto de
la IA + nota de trazabilidad armada por el código.

EXCEPCIÓN A LA REGLA DE `scripts/` (ver scripts/README.md):
    Este script lee la bitácora vigente de los pacientes con la service role,
    porque corre sin usuario. Solo hace esto:
      - lee `log_entries_vigentes` (entradas no anuladas) de las últimas 24 h;
      - escribe filas nuevas en `summaries` (estado generando -> listo | error);
      - envía el contexto al LLM como en la API (transitorio, sin persistir allá).
    No escribe nada más, no modifica entradas, ni expone el contenido en logs.

CÓMO SE EJECUTA:
    - .github/workflows/resumen-diario.yml (cron diario + disparo manual).
    - Requiere: SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY,
      GEMINI_API_KEY (o ANTHROPIC_API_KEY si LLM_PROVIDER=claude).
    - Idempotente por día: si el paciente ya tiene un resumen 'listo' o
      'generando' creado hoy (UTC), no se genera otro.
    - Pausa entre llamadas (RESUMEN_PAUSA_SEG, default 5 s) para respetar la
      cuota gratuita del LLM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

# Permite `from app...` al ejecutar `python scripts/resumen_diario.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supabase import Client, create_client  # noqa: E402

from app.resumen import construir_resumen  # noqa: E402

log = logging.getLogger("resumen_diario")
VENTANA = timedelta(hours=24)


def inicio_del_dia(ahora: datetime) -> str:
    return ahora.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def pacientes_con_actividad(client: Client, desde: datetime) -> list[str]:
    """IDs de pacientes con al menos una entrada vigente desde `desde`."""
    filas = (
        client.table("log_entries_vigentes")
        .select("patient_id")
        .eq("anulada", False)
        .gte("occurred_at", desde.isoformat())
        .execute()
        .data
    )
    return sorted({f["patient_id"] for f in filas})


def ya_generado_hoy(client: Client, patient_id: str, ahora: datetime) -> bool:
    filas = (
        client.table("summaries")
        .select("id")
        .eq("patient_id", patient_id)
        .gte("created_at", inicio_del_dia(ahora))
        .in_("estado", ["listo", "generando"])
        .limit(1)
        .execute()
        .data
    )
    return bool(filas)


async def generar_para(client: Client, patient_id: str, ahora: datetime) -> str:
    """Devuelve 'listo' o 'error'. Nunca registra contenido clínico."""
    desde = ahora - VENTANA
    fila = (
        client.table("summaries")
        .insert(
            {
                "patient_id": patient_id,
                "desde": desde.isoformat(),
                "hasta": ahora.isoformat(),
                "estado": "generando",
                "contenido": None,
            }
        )
        .execute()
        .data[0]
    )
    try:
        contenido = await construir_resumen(client, UUID(patient_id), desde, ahora)
        client.table("summaries").update({"contenido": contenido, "estado": "listo"}).eq(
            "id", fila["id"]
        ).execute()
        return "listo"
    except Exception as exc:  # noqa: BLE001 - el fallo queda marcado, no se pierde
        log.error("resumen de paciente %s falló: %s", patient_id, type(exc).__name__)
        client.table("summaries").update({"estado": "error"}).eq("id", fila["id"]).execute()
        return "error"


async def generar_diario(client: Client, ahora: datetime | None = None) -> dict[str, int]:
    ahora = ahora or datetime.now(UTC)
    pausa = float(os.environ.get("RESUMEN_PAUSA_SEG", "5"))
    conteo = {"pacientes": 0, "omitidos": 0, "listos": 0, "errores": 0}

    for patient_id in pacientes_con_actividad(client, ahora - VENTANA):
        conteo["pacientes"] += 1
        if ya_generado_hoy(client, patient_id, ahora):
            conteo["omitidos"] += 1
            continue
        estado = await generar_para(client, patient_id, ahora)
        conteo["listos" if estado == "listo" else "errores"] += 1
        await asyncio.sleep(pausa)
    return conteo


def main() -> int:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("Faltan SUPABASE_URL y/o SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    conteo = asyncio.run(generar_diario(create_client(url, key)))
    print(
        f"[resumen_diario] pacientes={conteo['pacientes']} omitidos={conteo['omitidos']} "
        f"listos={conteo['listos']} errores={conteo['errores']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
