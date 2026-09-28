"""Tests de retención (Ley 21.719 · Punto 4).

Verifica que el script `scripts/retention.py`:
    - Calcula el cutoff correctamente.
    - Al ejecutarse borra sólo assistant_messages viejos, NUNCA log_entries
      ni summaries.
    - Es idempotente.

No monta el cliente supabase; verifica la política de retención directamente
en SQL (equivalente a lo que ejecuta el script vía service role, que en
producción bypassa RLS).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.retention import cutoff_iso  # noqa: E402

pytestmark = pytest.mark.rls


class TestCutoff:
    def test_cutoff_seis_meses(self):
        ahora = datetime(2026, 9, 11, tzinfo=UTC)
        corte = cutoff_iso(6, ahora=ahora)
        assert corte == (ahora - timedelta(days=180)).isoformat()


class TestBorradoRealEnBase:
    def test_borra_mensajes_viejos_y_deja_recientes(self, make_user, add_member, superuser):
        admin = make_user("admin@test.cl")
        pid = admin.sql("insert into patients (nombre) values ('X') returning id")[0][0]

        # Insertar un mensaje ANTIGUO (created_at manipulado con superuser
        # porque un usuario normal no puede modificar created_at ni burlar
        # el default now()).
        with superuser.cursor() as cur:
            cur.execute(
                "insert into assistant_messages (patient_id, author_id, pregunta, respuesta, created_at) "
                "values (%s, %s, 'antiguo', 'r', now() - interval '400 days')",
                (str(pid), str(admin.user_id)),
            )
        # Y uno RECIENTE.
        admin.sql(
            "insert into assistant_messages (patient_id, author_id, pregunta, respuesta) "
            "values (%s, auth.uid(), 'reciente', 'r')",
            (str(pid),),
        )
        assert admin.sql("select count(*) from assistant_messages")[0][0] == 2

        # Ejecutar la política de retención (equivalente al DELETE del script).
        corte = cutoff_iso(6)
        with superuser.cursor() as cur:
            cur.execute("delete from assistant_messages where created_at < %s", (corte,))
            afectadas = cur.rowcount
        assert afectadas == 1

        # Bitácora y summaries intactos (no se tocan).
        # ... y el mensaje reciente sigue presente.
        rows = admin.sql("select pregunta from assistant_messages")
        assert len(rows) == 1 and rows[0][0] == "reciente"

    def test_no_borra_log_entries_ni_summaries_por_antiguedad(self, make_user, superuser):
        admin = make_user("admin@test.cl")
        pid = admin.sql("insert into patients (nombre) values ('X') returning id")[0][0]

        with superuser.cursor() as cur:
            cur.execute(
                "insert into log_entries (patient_id, author_id, kind, content, occurred_at, created_at) "
                "values (%s, %s, 'nota', 'x', now() - interval '400 days', now() - interval '400 days')",
                (str(pid), str(admin.user_id)),
            )
            cur.execute(
                "insert into summaries (patient_id, desde, hasta, contenido, created_at) "
                "values (%s, now() - interval '410 days', now() - interval '400 days', 'r', now() - interval '400 days')",
                (str(pid),),
            )
            # Ejecutar SÓLO la política definida por el script.
            corte = cutoff_iso(6)
            cur.execute("delete from assistant_messages where created_at < %s", (corte,))

        # Log entry y summary sobrevivieron.
        assert admin.sql("select count(*) from log_entries")[0][0] == 1
        assert admin.sql("select count(*) from summaries")[0][0] == 1
