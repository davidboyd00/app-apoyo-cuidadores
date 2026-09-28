"""Fixtures para tests de RLS.

Crea una base Postgres limpia, corre `db/auth_mock.sql` + `db/schema.sql`,
y expone conexiones que "actúan como" distintos usuarios usando
`set role authenticated` + `request.jwt.claim.sub`, tal como lo hace
Supabase con un JWT firmado.

Conexión: por defecto usa las variables estándar de libpq
(PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE). En local, sin nada seteado,
conecta por socket UNIX con el usuario del sistema — funciona out of the
box con Postgres instalado por brew. En CI, el workflow setea las PG*
apuntando al service container.

Overrides opcionales:
    TEST_PG_ADMIN_DSN   DSN completa (sobrescribe todo lo anterior).
    TEST_PG_DB          nombre de la BD de test (default caregivers_rls_test).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = (ROOT / "db" / "schema.sql").read_text()
AUTH_MOCK_SQL = (ROOT / "db" / "auth_mock.sql").read_text()
SEED_PLACEHOLDER_SQL = (ROOT / "db" / "seed_placeholder.sql").read_text()

TEST_DB = os.environ.get("TEST_PG_DB", "caregivers_rls_test")
ADMIN_DSN = os.environ.get("TEST_PG_ADMIN_DSN")  # opcional: sobrescribe PG*


def _connect(dbname: str) -> psycopg.Connection:
    """Conecta a `dbname`. Si hay TEST_PG_ADMIN_DSN lo usa (con dbname
    sobreescrito); si no, delega en libpq (PG* env vars) — sin dsn,
    psycopg usa PGHOST/PGPORT/PGUSER/PGPASSWORD del ambiente."""
    if ADMIN_DSN:
        return psycopg.connect(ADMIN_DSN, dbname=dbname, autocommit=True)
    # PGDATABASE puede estar seteada; la sobrescribimos con dbname explícito.
    return psycopg.connect(dbname=dbname, autocommit=True)


@pytest.fixture(scope="session")
def base_dsn() -> str:
    """Crea una base limpia por sesión con schema + policies aplicados.
    Retorna un identificador (dbname) que las fixtures usan para conectar."""
    with _connect("postgres") as conn, conn.cursor() as cur:
        cur.execute(f"drop database if exists {TEST_DB} with (force)")
        cur.execute(f"create database {TEST_DB}")
    with _connect(TEST_DB) as conn, conn.cursor() as cur:
        cur.execute(AUTH_MOCK_SQL)
        cur.execute(SCHEMA_SQL)
        # Placeholder de "usuario eliminado" — en Supabase real se crea a
        # mano con el INSERT expandido de docs/SUPABASE.md.
        cur.execute(SEED_PLACEHOLDER_SQL)
    yield TEST_DB
    with _connect("postgres") as conn, conn.cursor() as cur:
        cur.execute(f"drop database if exists {TEST_DB} with (force)")


@pytest.fixture
def superuser(base_dsn):
    """Conexión con permisos totales (para setup y verificación cruda)."""
    with _connect(base_dsn) as conn:
        yield conn


@pytest.fixture
def clean_data(superuser):
    """Trunca datos de negocio entre tests. Preserva el usuario placeholder
    "eliminado" del schema (ver db/schema.sql § Ley 21.719)."""
    with superuser.cursor() as cur:
        cur.execute(
            "truncate patients, care_members, log_entries, medications, "
            "summaries, assistant_messages restart identity cascade"
        )
        # Borra usuarios de test pero deja el placeholder.
        cur.execute("delete from auth.users where id <> '00000000-0000-0000-0000-000000000000'")
    yield


class UserSession:
    """Wrapper que abre una conexión y "actúa como" un usuario dado.

    Equivalente a lo que hace la API cuando recibe un JWT: `set role
    authenticated` + inyectar el sub del token en la sesión.
    """

    def __init__(self, dbname: str, user_id: UUID):
        self.user_id = user_id
        self.conn = _connect(dbname)
        with self.conn.cursor() as cur:
            cur.execute("set role authenticated")
            cur.execute("select set_config('request.jwt.claim.sub', %s, false)", (str(user_id),))

    def sql(self, query: str, params: tuple | None = None):
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            if cur.description:
                return cur.fetchall()
            return None

    def close(self):
        self.conn.close()


@pytest.fixture
def make_user(base_dsn, superuser, clean_data):
    """Crea un usuario en auth.users y retorna un UserSession que actúa como él."""
    sessions: list[UserSession] = []

    def _make(email: str) -> UserSession:
        with superuser.cursor() as cur:
            cur.execute("insert into auth.users (email) values (%s) returning id", (email,))
            uid = cur.fetchone()[0]
        s = UserSession(base_dsn, uid)
        sessions.append(s)
        return s

    yield _make
    for s in sessions:
        s.close()


@pytest.fixture
def add_member(superuser):
    """Agrega un usuario a un grupo de cuidado con un rol dado (bypass RLS)."""

    def _add(patient_id: UUID, user_id: UUID, role: str) -> None:
        with superuser.cursor() as cur:
            cur.execute(
                "insert into care_members (patient_id, user_id, role) values (%s, %s, %s) "
                "on conflict (patient_id, user_id) do update set role = excluded.role",
                (str(patient_id), str(user_id), role),
            )

    return _add
