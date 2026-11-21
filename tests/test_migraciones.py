"""El paquete db/APLICAR_EN_SUPABASE.sql, aplicado sobre el schema que tiene
producción, debe dejar la base IGUAL a la foto db/schema.sql (y conservar los
datos). Así, pegarlo en Supabase no tiene sorpresas.

Se comparan catálogos normalizados (el orden de las columnas no importa: un
ALTER TABLE las agrega al final y la foto las declara en su lugar).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest

from tests.conftest import AUTH_MOCK_SQL, SCHEMA_SQL, SEED_PLACEHOLDER_SQL, _connect

pytestmark = pytest.mark.rls

RAIZ = Path(__file__).resolve().parent.parent
PAQUETE = RAIZ / "db" / "APLICAR_EN_SUPABASE.sql"
PRODUCCION = (Path(__file__).parent / "fixtures" / "schema_produccion_20261019.sql").read_text()

USUARIA = "11111111-1111-1111-1111-111111111111"
PACIENTE = "22222222-2222-2222-2222-222222222222"
ENTRADA = "33333333-3333-3333-3333-333333333333"

CATALOGO = {
    "columnas": """
        select table_name, column_name, data_type, udt_name, is_nullable,
               coalesce(column_default, '')
        from information_schema.columns where table_schema = 'public'""",
    "restricciones": """
        select conrelid::regclass::text, conname, pg_get_constraintdef(oid)
        from pg_constraint where connamespace = 'public'::regnamespace""",
    "indices": "select tablename, indexname, indexdef from pg_indexes where schemaname = 'public'",
    "policies": """
        select tablename, policyname, cmd, permissive, roles::text,
               coalesce(qual, ''), coalesce(with_check, '')
        from pg_policies where schemaname = 'public'""",
    "vistas": """
        select c.relname, pg_get_viewdef(c.oid, true), coalesce(array_to_string(c.reloptions, ','), '')
        from pg_class c where c.relnamespace = 'public'::regnamespace and c.relkind = 'v'""",
    "funciones": """
        select p.proname, pg_get_function_identity_arguments(p.oid),
               md5(regexp_replace(p.prosrc, '\\s+', ' ', 'g')), p.prosecdef, p.provolatile,
               coalesce(array_to_string(p.proconfig, ','), '')
        from pg_proc p where p.pronamespace = 'public'::regnamespace""",
    "triggers": """
        select tgrelid::regclass::text, tgname, pg_get_triggerdef(oid) from pg_trigger
        where not tgisinternal
          and tgrelid in (select oid from pg_class where relnamespace = 'public'::regnamespace)""",
    "rls": """
        select relname, relrowsecurity from pg_class
        where relnamespace = 'public'::regnamespace and relkind = 'r'""",
    "permisos": """
        select table_name, grantee, privilege_type from information_schema.role_table_grants
        where table_schema = 'public' and grantee in ('authenticated', 'anon')""",
    "enums": """
        select t.typname, string_agg(e.enumlabel, ',' order by e.enumsortorder)
        from pg_type t join pg_enum e on e.enumtypid = t.oid group by t.typname""",
}


def _catalogo(dbname: str) -> dict[str, set]:
    with _connect(dbname) as conn, conn.cursor() as cur:
        out = {}
        for nombre, sql in CATALOGO.items():
            cur.execute(sql)
            out[nombre] = set(cur.fetchall())
        return out


def _base(nombre: str, *scripts: str) -> str:
    with _connect("postgres") as conn, conn.cursor() as cur:
        cur.execute(f"drop database if exists {nombre} with (force)")
        cur.execute(f"create database {nombre}")
    with _connect(nombre) as conn, conn.cursor() as cur:
        for s in scripts:
            cur.execute(s)
    return nombre


def _borrar(*nombres: str) -> None:
    with _connect("postgres") as conn, conn.cursor() as cur:
        for n in nombres:
            cur.execute(f"drop database if exists {n} with (force)")


def _sembrar_datos_de_produccion(dbname: str) -> None:
    """Datos como los que hoy existen en producción (antes del paquete)."""
    with _connect(dbname) as conn, conn.cursor() as cur:
        cur.execute("insert into auth.users (id, email) values (%s, 'ana@test.cl')", (USUARIA,))
        cur.execute("select set_config('request.jwt.claim.sub', %s, false)", (USUARIA,))
        cur.execute("insert into patients (id, nombre) values (%s, 'Doña Elba')", (PACIENTE,))
        cur.execute(
            "insert into log_entries (id, patient_id, author_id, kind, content, occurred_at) "
            "values (%s, %s, %s, 'nota', 'Durmió bien', now())",
            (ENTRADA, PACIENTE, USUARIA),
        )
        cur.execute(
            "insert into summaries (patient_id, desde, hasta, contenido) "
            "values (%s, now() - interval '7 days', now(), 'Resumen previo')",
            (PACIENTE,),
        )
        cur.execute(
            "insert into assistant_messages (patient_id, author_id, pregunta, respuesta, "
            "cited_entry_ids) values (%s, %s, 'p', 'r', %s)",
            (PACIENTE, USUARIA, [ENTRADA]),
        )


def _cargar_paquete_desde_script():
    spec = importlib.util.spec_from_file_location(
        "armar_paquete", RAIZ / "scripts" / "armar_paquete_migraciones.py"
    )
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_paquete_esta_al_dia_con_las_migraciones():
    modulo = _cargar_paquete_desde_script()
    assert PAQUETE.read_text() == modulo.armar(), (
        "db/APLICAR_EN_SUPABASE.sql está desactualizado: corre "
        "`python scripts/armar_paquete_migraciones.py`"
    )


@pytest.fixture(scope="module")
def bases():
    prod = _base("caregivers_mig_prod", AUTH_MOCK_SQL, PRODUCCION, SEED_PLACEHOLDER_SQL)
    _sembrar_datos_de_produccion(prod)
    paquete = PAQUETE.read_text()
    with _connect(prod) as conn, conn.cursor() as cur:
        cur.execute(paquete)
        cur.execute(paquete)  # idempotente: una segunda pasada no rompe nada
    foto = _base("caregivers_mig_foto", AUTH_MOCK_SQL, SCHEMA_SQL, SEED_PLACEHOLDER_SQL)
    yield prod, foto
    _borrar(prod, foto)


@pytest.mark.parametrize("parte", sorted(CATALOGO))
def test_paquete_sobre_produccion_equivale_a_la_foto(bases, parte):
    prod, foto = bases
    con_paquete = _catalogo(prod)[parte]
    esperado = _catalogo(foto)[parte]
    assert con_paquete - esperado == set(), f"{parte}: sobra tras el paquete"
    assert esperado - con_paquete == set(), f"{parte}: falta tras el paquete"


def test_verificacion_del_paquete_queda_en_verde(bases):
    prod, _ = bases
    verificacion = PAQUETE.read_text().split("commit;", 1)[1]
    with _connect(prod) as conn, conn.cursor() as cur:
        cur.execute(verificacion)
        filas = cur.fetchall()
    assert filas and all(ok for _, ok in filas), filas


def test_datos_existentes_sobreviven(bases):
    prod, _ = bases
    with _connect(prod) as conn, conn.cursor() as cur:
        cur.execute(
            "select content, anulada, editada from log_entries_vigentes where id = %s", (ENTRADA,)
        )
        assert cur.fetchone() == ("Durmió bien", False, False)
        cur.execute("select estado, contenido from summaries")
        assert cur.fetchall() == [("listo", "Resumen previo")]
        # Lo que antes se guardaba en cited_entry_ids era el contexto completo.
        cur.execute(
            "select cited_entry_ids, contexto_entry_ids, suficiente from assistant_messages"
        )
        assert cur.fetchall() == [([UUID(ENTRADA)], [UUID(ENTRADA)], None)]
