# caregivers-backend

Backend FastAPI de la app de apoyo a cuidadores (Grupo 7 · GPTI PUC 2026-2).

- Arquitectura: `ARCHITECTURE.md`
- Reglas de trabajo: `CLAUDE.md`
- Cumplimiento Ley 21.719: `docs/`

## Cómo correr local

Requiere Python 3.11+ y (para tests RLS) Postgres 16 local.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # completar con keys de Supabase
uvicorn app.main:app --reload   # docs en http://localhost:8000/docs
```

## Tests

```bash
# los tests de RLS levantan una BD local, corren db/auth_mock.sql + db/schema.sql
pytest -v
```

Parametrizable con dos env vars (con defaults sensatos para socket local):

- `TEST_PG_ADMIN_DSN` — DSN de un superusuario que pueda crear/borrar bases
  (default `postgresql:///postgres`).
- `TEST_PG_DB` — nombre de la BD de test (default `caregivers_rls_test`).

En CI (GitHub Actions) el servicio `postgres:16` expone `postgres` con auth
trust en `localhost:5432`; el workflow setea `TEST_PG_ADMIN_DSN` acordemente
(ver `.github/workflows/ci.yml`).

## Lint y formato

```bash
ruff check .
ruff format .
```

## Estructura

```
app/
  main.py               arranque, errores globales, gate de consentimiento
  config.py             settings (.env)
  deps.py               JWT + cliente Supabase con RLS + require_consent
  schemas.py            contrato Pydantic
  llm.py                capa LLM (Gemini/Claude, contexto cerrado)
  audit.py              helper para escribir en audit_log vía RPC
  errors.py             handlers HTTP en español (RequestValidationError,
                        PostgREST APIError)
  routers/              patients, entries, medications, assistant, summaries,
                        metrics, centers, me, audit
db/
  schema.sql            fuente de verdad del schema (tablas + RLS + funciones
                        SECURITY DEFINER + comments de minimización)
  auth_mock.sql         mock local de auth.users + auth.uid() (Supabase lo
                        provee en producción; este archivo NO se corre allá)
scripts/
  retention.py          Ley 21.719: cron mensual purga assistant_messages
                        > N meses (RETENTION_MONTHS_ASSISTANT, default 6)
  README.md
docs/
  POLITICA_PRIVACIDAD.md
  REGISTRO_TRATAMIENTO.md
  DPIA.md
  PLAN_BRECHAS.md
tests/
  conftest.py           fixtures que crean BD limpia por sesión
  test_rls.py           17 tests de policies (aislamiento, roles, historial)
  test_arco.py          delete_my_account + cascade sin huérfanos
  test_audit.py         audit_log append-only + RLS
  test_consent.py       consents append-only + require_consent HTTP
  test_conversations.py agrupamiento por conversation_id
  test_retention.py     cutoff + solo borra assistant_messages
  test_errors.py        handlers HTTP en español
.github/workflows/
  ci.yml                lint + tests (Postgres 16 service)
  retention.yml         cron mensual → scripts/retention.py
```

## Endpoints (resumen)

Los detalles están en `/docs` (OpenAPI). Este resumen es para orientarse:

| Área                | Endpoints                                                                 |
|---------------------|---------------------------------------------------------------------------|
| Pacientes           | `GET|POST /patients`, `GET|DELETE /patients/{id}`                         |
| Bitácora            | `GET|POST /patients/{id}/entries`, `PATCH|DELETE /entries/{eid}`          |
| Medicamentos        | `GET|POST /patients/{id}/medications`, `PATCH`, `POST /desactivar`        |
| Asistente IA        | `POST /assistant/ask`, `GET /messages`, `GET /conversations`,             |
|                     | `GET /conversations/{cid}/messages`                                       |
| Resúmenes           | `GET|POST /patients/{id}/summaries`                                       |
| Métricas            | `GET /patients/{id}/metrics/relevo`                                        |
| Auditoría (admin)   | `GET /patients/{id}/audit`                                                |
| Centros (público)   | `GET /centers`                                                            |
| Consentimiento      | `GET|POST /me/consent`                                                     |
| Derechos ARCO       | `GET /me/data-export`, `DELETE /me?confirmar=true`                        |
| Salud               | `GET /health`                                                              |

## Reglas duras

Ver `CLAUDE.md` "Reglas sagradas". Resumen operativo:

1. La API **nunca** usa la service role key.
2. Autorización vive en RLS (`is_member` / `has_role`, ambas SECURITY
   DEFINER). No agregues checks de rol ad-hoc en Python.
3. El LLM recibe contexto cerrado (bitácora autorizada por RLS), nunca
   toca la base ni tiene function-calling.
4. Toda respuesta del asistente persiste `cited_entry_ids`.
5. Toda entrada de bitácora persiste `author_id` (default `auth.uid()`).
6. Audio nunca llega al backend.
7. Ningún log de aplicación ni fila de `audit_log` contiene contenido
   clínico. Solo ids/counts/flags.

## Pendientes conocidos

- **Jurídicos** (recogidos en los `docs/`): texto real de la política,
  contratos de encargo con proveedores (Supabase, LLM, Render), definir
  plazos de retención de `audit_log`, evaluar transferencias
  internacionales, definir roles DPO/coordinación.
- **Producto**: scrapers reales (`scripts/*.py` de otro integrante),
  notificaciones/recordatorios de medicamentos.
