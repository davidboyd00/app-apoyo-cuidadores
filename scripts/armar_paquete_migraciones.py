"""Arma db/APLICAR_EN_SUPABASE.sql: las migraciones pendientes en un solo archivo.

Uso:  python scripts/armar_paquete_migraciones.py

Todo corre en UNA transacción (si algo falla no se aplica nada) y al final
muestra una tabla de verificación. Cada migración es idempotente, así que el
paquete también lo es. tests/test_migraciones.py verifica que el archivo esté
al día con esta lista y que, aplicado sobre el schema de producción, deje la
base igual a db/schema.sql.

Al aplicar el paquete en Supabase, marcar las migraciones como "aplicado" en
db/migrations/README.md y vaciar PENDIENTES.
"""

from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
MIGRACIONES = RAIZ / "db" / "migrations"
DESTINO = RAIZ / "db" / "APLICAR_EN_SUPABASE.sql"

# Semillas que la base necesita y que no son schema (idempotentes). El usuario
# anónimo recibe la autoría de los registros cuando alguien elimina su cuenta:
# sin él, eliminar una cuenta con registros falla por la llave foránea.
SEMILLAS = ["seed_placeholder.sql"]

# En orden de aplicación.
PENDIENTES = [
    "20261103_historial_inmutable_offline.sql",
    "20261118_resumen_asincrono.sql",
    "20261118_vista_trazabilidad.sql",
    "20261121_asistente_estructurado.sql",
    "20261121_turnos.sql",
]

ENCABEZADO = """-- ============================================================================
-- PAQUETE DE MIGRACIONES PENDIENTES · aplicar UNA vez en Supabase
-- Generado por scripts/armar_paquete_migraciones.py (no editar a mano).
--
-- Incluye, en orden:
{lista}
--   + semilla: usuario anónimo (db/seed_placeholder.sql)
--
-- Cómo aplicarlo (2 minutos):
--   1. Supabase → SQL Editor → New query.
--   2. Pegar este archivo COMPLETO y presionar Run.
--   3. Corre en una sola transacción: si algo falla, no se aplica nada y
--      la base queda como estaba.
--   4. Al final aparece una tabla `verificacion`: todas las filas deben
--      decir ok = true.
--   5. Marcar las migraciones como "aplicado" en db/migrations/README.md.
--
-- Es idempotente: correrlo dos veces no rompe nada. Requiere PostgreSQL 15+.
-- ============================================================================

begin;
"""

VERIFICACION = """
commit;

-- Verificación (solo lectura). Todas las filas deben decir ok = true.
select item, ok from (values
  ('tabla log_entry_revisions', to_regclass('public.log_entry_revisions') is not null),
  ('vista log_entries_vigentes con trazabilidad', exists (
     select 1 from information_schema.columns
     where table_schema = 'public' and table_name = 'log_entries_vigentes'
       and column_name = 'ultima_revision_autor_id')),
  ('bitácora insert-only (sin policies UPDATE/DELETE)', not exists (
     select 1 from pg_policies
     where schemaname = 'public' and tablename = 'log_entries' and cmd in ('UPDATE', 'DELETE'))),
  ('carga offline (log_entries.client_id)', exists (
     select 1 from information_schema.columns
     where table_schema = 'public' and table_name = 'log_entries' and column_name = 'client_id')),
  ('resúmenes asíncronos (summaries.estado)', exists (
     select 1 from information_schema.columns
     where table_schema = 'public' and table_name = 'summaries' and column_name = 'estado')),
  ('asistente estructurado (contexto_entry_ids)', exists (
     select 1 from information_schema.columns
     where table_schema = 'public' and table_name = 'assistant_messages'
       and column_name = 'contexto_entry_ids')),
  ('turnos con RLS (care_shifts)', coalesce((
     select relrowsecurity from pg_class where oid = to_regclass('public.care_shifts')), false)),
  ('helper puede_estar_de_turno', to_regprocedure('public.puede_estar_de_turno(uuid,uuid)') is not null),
  ('usuario anónimo para eliminar cuentas', exists (
     select 1 from auth.users where id = '00000000-0000-0000-0000-000000000000'
       and aud = 'authenticated' and confirmation_token is not null))
) as verificacion(item, ok);
"""


def armar() -> str:
    lista = "\n".join(f"--   {i}. {n}" for i, n in enumerate(PENDIENTES, 1))
    partes = [ENCABEZADO.format(lista=lista)]
    for i, nombre in enumerate(PENDIENTES, 1):
        cuerpo = (MIGRACIONES / nombre).read_text().strip()
        partes.append(f"\n-- >>>>>>>>>> {i}/{len(PENDIENTES)} · {nombre}\n\n{cuerpo}\n")
    for nombre in SEMILLAS:
        cuerpo = (RAIZ / "db" / nombre).read_text().strip()
        partes.append(f"\n-- >>>>>>>>>> semilla · db/{nombre}\n\n{cuerpo}\n")
    partes.append(VERIFICACION)
    return "".join(partes)


if __name__ == "__main__":
    DESTINO.write_text(armar())
    print(f"escribí {DESTINO.relative_to(RAIZ)} con {len(PENDIENTES)} migraciones")
