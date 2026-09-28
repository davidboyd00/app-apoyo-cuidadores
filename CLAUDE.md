# CLAUDE.md — Backend App de apoyo a cuidadores (Grupo 7 · GPTI PUC 2026-2)

Instrucciones para trabajar en este repo. Léelas antes de escribir código.
La arquitectura completa y sus fundamentos están en `ARCHITECTURE.md` — ante
cualquier decisión de diseño, ese documento manda.

## Qué es este proyecto

Backend FastAPI de una app móvil (React Native/Expo) para cuidadores de
personas dependientes: bitácora compartida entre familiares + asistente de IA
que responde SOLO sobre lo registrado + resúmenes médicos automáticos.
Proyecto universitario con presupuesto $0 (capas gratuitas), entrega final
24/11/2026. Responsable backend: David Boyd.

## Stack (no cambiar sin discusión)

- **API**: FastAPI (Python 3.11+), deploy en Render free tier
- **Datos/Auth**: Supabase (PostgreSQL + Auth + RLS + Realtime)
- **LLM**: Gemini free tier por defecto, Claude como alternativa (`app/llm.py`)
- **Jobs**: scrapers por GitHub Actions cron (`scripts/`)

## Cómo correr

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt              # incluye pytest, ruff, psycopg
cp .env.example .env                             # completar con keys de Supabase
uvicorn app.main:app --reload                    # docs en localhost:8000/docs

# tests (requiere Postgres 16 local)
pytest -v
# tests contra otro Postgres:
TEST_PG_ADMIN_DSN=postgresql://user@host:5432/postgres pytest

# lint
ruff check .
ruff format .
```

El schema de la base está en `db/schema.sql` y se corre en el SQL Editor
de Supabase. Si cambias el modelo de datos, actualiza ese archivo (es la
fuente de verdad del schema, no hay migraciones automáticas por ahora).
`db/auth_mock.sql` es solo para tests locales/CI — Supabase provee el
schema `auth` real en producción.

## Reglas sagradas (violarlas rompe seguridad o KPIs comprometidos)

0. **Los checks de rol en policies RLS SIEMPRE van vía funciones SECURITY
   DEFINER** (`is_member(pid)`, `has_role(pid, r)` en `db/schema.sql`).
   Nunca `EXISTS (SELECT ... FROM care_members)` inline en un policy: se
   auto-referencia con RLS activo y produce recursión o negación silenciosa.
1. **Nunca uses la service role key en la API.** La API consulta Supabase
   con el token del usuario (`get_db` en `app/deps.py`) para que las
   políticas RLS apliquen. La service role vive SOLO en `scripts/` (jobs
   como `retention.py`).
2. **La autorización vive en RLS, no en el código.** No agregues checks de
   permisos ad-hoc en endpoints como reemplazo de una política; si falta una
   política, se agrega en `db/schema.sql`.
3. **El LLM nunca toca la base.** El patrón es contexto cerrado: la API arma
   el contexto con la bitácora autorizada y se lo pasa. Nada de
   function-calling con acceso a datos, nada de SQL generado por el modelo.
4. **Toda respuesta del asistente registra `cited_entry_ids`** (qué entradas
   de bitácora vio). Es lo que hace medible el KPI de >90% verificable.
5. **Toda entrada de bitácora registra `author_id`.** Es lo que hace medible
   el KPI de relevo familiar (>60%).
6. **No proceses audio en el backend.** La voz se transcribe en el cliente
   y llega como texto con `source: "voice"`.
7. **Datos de salud = dato sensible (Ley 21.719).** No agregues campos ni
   logs que expongan datos clínicos innecesariamente; los datos del paciente
   no se persisten en servicios de terceros (el LLM los recibe como contexto
   transitorio, no los almacenamos allá). `audit_log.metadata` acepta solo
   ids/counts/flags — jamás `content`, `respuesta`, `pregunta` ni notas.
8. **`consents` y `audit_log` son append-only.** No tienen policies
   `UPDATE`/`DELETE` a propósito: la evidencia del consentimiento y de las
   acciones sensibles debe ser inmodificable.

## Convenciones

- Código y comentarios en español (es un proyecto chileno y así lo evalúan).
- Endpoints REST anidados por paciente: `/patients/{id}/entries`, etc.
- Pydantic en `app/schemas.py` es el contrato con el front — cambios ahí se
  avisan a Desarrollo Móvil (María Paz).
- Escrituras a la bitácora son append-only (no updates concurrentes).
- Endpoints que llaman al LLM son `async` y con `max_tokens` acotado.
- Errores HTTP en español y accionables.

## Estructura

```
app/
  main.py      arranque, handlers de error, gate de consentimiento
  config.py    settings desde .env (pydantic-settings) — incluye POLICY_VERSION
  deps.py      JWT + cliente Supabase con RLS + require_consent
  schemas.py   contrato Pydantic (avisar a Móvil ante cambios)
  llm.py       capa LLM (proveedor abstraído)
  errors.py    handlers HTTP en español
  audit.py     registrar(...) → RPC audit_write
  routers/     patients, entries, medications, assistant, summaries,
               metrics, centers, me (data-export/delete/consent), audit
db/
  schema.sql   fuente de verdad (tablas + RLS + funciones + comments de
               minimización L21.719)
  auth_mock.sql  mock local; NO se corre en Supabase
scripts/       retention.py (backend, service role) + scrapers de otro dev
docs/          POLITICA + REGISTRO_TRATAMIENTO + DPIA + PLAN_BRECHAS
tests/         RLS + ARCO + audit + consent + conversations + retention +
               errores (todos verdes en <5s, requiere Postgres 16 local)
.github/workflows/  ci.yml (lint + tests) + retention.yml (cron mensual)
```

Cambios de contrato en `app/schemas.py` se avisan a Desarrollo Móvil
(María Paz). Cambios en `db/schema.sql` que agreguen categorías de dato
requieren actualizar `docs/REGISTRO_TRATAMIENTO.md` en el mismo PR.

## Qué NO hacer

- No introducir microservicios, colas, embeddings/RAG ni websockets propios:
  están descartados con argumento en `ARCHITECTURE.md` (sección 8). Si crees
  que hace falta, propónlo, no lo implementes de una.
- No agregar dependencias pesadas sin necesidad (presupuesto $0 y Render free).
- No commitear `.env` ni keys.

## Pendientes conocidos

- **Jurídicos** (recogidos en los `docs/`, marcados PENDIENTE JURÍDICO):
  texto real de política, contratos de encargo (Supabase/Render/LLM),
  consentimiento explícito para categoría especial, plazos de retención de
  `audit_log`, transferencias internacionales, roles DPO/coordinación.
- **Producto**: scrapers reales (`scripts/*.py` de otro integrante),
  notificaciones/recordatorios de medicamentos.
- **Ops**: procedimiento formal de rotación de claves Supabase.
