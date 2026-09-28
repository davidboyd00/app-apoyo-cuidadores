"""Ley 21.719 · Punto 4: retención acotada de datos derivados.

Borra `assistant_messages` (conversaciones IA) con más de N meses de
antigüedad. Configurable por `RETENTION_MONTHS_ASSISTANT` (default: 6).

QUÉ NO SE BORRA (por diseño):
    - `log_entries`: bitácora clínica, base de licitud = interés legítimo
      del grupo de cuidado y salud del paciente. Se conserva mientras el
      grupo exista; borrado se ejerce vía DELETE /me y DELETE /patients/{id}.
    - `summaries`: resúmenes médicos son documentación clínica, no
      derivado transitorio; se conservan.

CÓMO SE EJECUTA:
    - Cron de GitHub Actions (.github/workflows/retention.yml).
    - USA `SUPABASE_SERVICE_ROLE_KEY` (bypass RLS) porque debe borrar
      transversalmente todos los pacientes. Este script vive en `scripts/`
      justamente por eso — la API NO usa esta key (regla sagrada #1).
    - Idempotente: si no hay filas antiguas, no hace nada.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from supabase import create_client


def cutoff_iso(meses: int, ahora: datetime | None = None) -> str:
    """Fecha ISO 8601 desde la cual todo lo anterior se borra."""
    ahora = ahora or datetime.now(UTC)
    # 30 días/mes como aproximación operativa: es una política de retención,
    # no una fecha calendario exacta. Documentado.
    return (ahora - timedelta(days=30 * meses)).isoformat()


def borrar_conversaciones_antiguas(client, meses: int) -> int:
    """Retorna cuántas filas fueron borradas."""
    corte = cutoff_iso(meses)
    res = client.table("assistant_messages").delete().lt("created_at", corte).execute()
    return len(res.data or [])


def main() -> int:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("Faltan SUPABASE_URL y/o SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
        return 2

    meses = int(os.environ.get("RETENTION_MONTHS_ASSISTANT", "6"))
    client = create_client(url, key)

    borradas = borrar_conversaciones_antiguas(client, meses)
    print(f"[retention] borradas {borradas} conversaciones IA (> {meses} meses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
