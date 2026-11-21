"""Stack local: Postgres con el schema real y RLS, PostgREST como en Supabase
y la API FastAPI (con un LLM falso). Lo usan las pruebas de integración
(EDT 5.1.1) y la demo local (`python -m tests.integracion.demo`).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
from jose import jwt

from tests.conftest import AUTH_MOCK_SQL, SCHEMA_SQL, SEED_PLACEHOLDER_SQL, _connect

RAIZ = Path(__file__).resolve().parents[2]
ROLES_SQL = (Path(__file__).parent / "roles_postgrest.sql").read_text()
DB = "caregivers_integracion"
SECRETO = "integracion-local-secreto-de-al-menos-32-caracteres"


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _token(sub: str, email: str | None, role: str = "authenticated") -> str:
    ahora = int(time.time())
    claims = {"sub": sub, "role": role, "aud": "authenticated", "iat": ahora, "exp": ahora + 7200}
    if email:
        claims["email"] = email
    return jwt.encode(claims, SECRETO, algorithm="HS256")


@dataclass
class Persona:
    id: str
    email: str
    token: str

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class Stack:
    api: str
    supabase: str
    db: str
    anon_key: str

    def persona(self, email: str) -> Persona:
        uid = str(uuid4())
        with _connect(self.db) as conn, conn.cursor() as cur:
            cur.execute("insert into auth.users (id, email) values (%s, %s)", (uid, email))
        return Persona(id=uid, email=email, token=_token(uid, email))

    def cliente(self) -> httpx.Client:
        return httpx.Client(base_url=self.api, timeout=30)


def _esperar(url: str, procesos: list, logs: list[Path], intentos: int = 100) -> None:
    for _ in range(intentos):
        for p, log in zip(procesos, logs, strict=True):
            if p.poll() is not None:
                raise RuntimeError(f"Un proceso del stack terminó:\n{log.read_text()[-3000:]}")
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"El stack no respondió en {url}")


@contextmanager
def levantar_stack(binario: str, puerto_api: int | None = None, puerto_supabase: int | None = None):
    """Levanta Postgres (base nueva) + PostgREST + proxy + API. Al salir, apaga
    todo y borra la base."""
    with _connect("postgres") as conn, conn.cursor() as cur:
        cur.execute(f"drop database if exists {DB} with (force)")
        cur.execute(f"create database {DB}")
    with _connect(DB) as conn, conn.cursor() as cur:
        for sql in (AUTH_MOCK_SQL, SCHEMA_SQL, SEED_PLACEHOLDER_SQL, ROLES_SQL):
            cur.execute(sql)

    tmp = Path(tempfile.mkdtemp(prefix="integracion-"))
    p_rest = _puerto_libre()
    p_proxy = puerto_supabase or _puerto_libre()
    p_api = puerto_api or _puerto_libre()
    host = os.environ.get("PGHOST", "localhost")
    puerto_pg = os.environ.get("PGPORT", "5432")
    conf = tmp / "postgrest.conf"
    conf.write_text(
        f'db-uri = "postgres://authenticator@{host}:{puerto_pg}/{DB}"\n'
        'db-schemas = "public"\n'
        'db-anon-role = "anon"\n'
        f'jwt-secret = "{SECRETO}"\n'
        f"server-port = {p_rest}\n"
        'server-host = "127.0.0.1"\n'
        "db-pool = 5\n"
    )
    anon_key = _token(str(uuid4()), None, role="anon")
    env = {
        **os.environ,
        "POSTGREST_URL": f"http://127.0.0.1:{p_rest}",
        "JWT_SECRETO": SECRETO,
        "SUPABASE_URL": f"http://127.0.0.1:{p_proxy}",
        "SUPABASE_ANON_KEY": anon_key,
        "SUPABASE_JWT_SECRET": SECRETO,
        "LLM_PROVIDER": "falso",
        "PYTHONPATH": str(RAIZ),
    }
    uvicorn = [sys.executable, "-m", "uvicorn", "--host", "127.0.0.1", "--log-level", "warning"]
    comandos = [
        ([binario, str(conf)], tmp / "postgrest.log"),
        ([*uvicorn, "--port", str(p_proxy), "tests.integracion.proxy:app"], tmp / "proxy.log"),
        ([*uvicorn, "--port", str(p_api), "tests.integracion.app_llm_falso:app"], tmp / "api.log"),
    ]
    procesos, logs = [], []
    try:
        for cmd, log in comandos:
            logs.append(log)
            procesos.append(
                subprocess.Popen(
                    cmd, cwd=RAIZ, env=env, stdout=log.open("w"), stderr=subprocess.STDOUT
                )
            )
        _esperar(f"http://127.0.0.1:{p_rest}/", procesos[:1], logs[:1])
        _esperar(f"http://127.0.0.1:{p_api}/health", procesos, logs)
        yield Stack(
            api=f"http://127.0.0.1:{p_api}",
            supabase=f"http://127.0.0.1:{p_proxy}",
            db=DB,
            anon_key=anon_key,
        )
    finally:
        for p in procesos:
            p.terminate()
        for p in procesos:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        with _connect("postgres") as conn, conn.cursor() as cur:
            cur.execute(f"drop database if exists {DB} with (force)")
        shutil.rmtree(tmp, ignore_errors=True)
