# Puesta en producción del MVP (checklist)

Pasos que requieren acceso a Supabase, Render o GitHub y que no se pueden
hacer desde el código. En orden. Cada paso dice cómo verificarlo.

> **Importante:** Render despliega `main` automáticamente (`render.yaml`,
> `autoDeploy: true`). Desde que `main` tiene la API de noviembre, la
> bitácora, el asistente y los resúmenes **fallan en producción hasta
> aplicar el paso 1**. Hacerlo primero.

## 1. Base de datos (Supabase) · 2 minutos

1. Supabase → **SQL Editor** → **New query**.
2. Pegar **completo** `db/APLICAR_EN_SUPABASE.sql` → **Run**.
   - Corre en una sola transacción: si algo falla, no se aplica nada.
   - Es idempotente: correrlo dos veces no rompe nada.
3. Verificar: al final aparece la tabla `verificacion`; **todas las filas
   deben decir `ok = true`**.
4. Marcar las 5 migraciones como "aplicado" en `db/migrations/README.md`.

Qué incluye: historial inmutable y carga offline (03/11), resúmenes
asíncronos y vista de trazabilidad (18/11), asistente estructurado y turnos
de cuidado (21/11). Probado contra una copia del schema de producción:
`tests/test_migraciones.py`.

## 2. API (Render) · variables de entorno

En el servicio `caregivers-backend` → **Environment**. `render.yaml` ya trae
los valores, pero si el servicio no se creó como Blueprint hay que ponerlos a
mano:

| Variable | Valor | Por qué |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.6-flash` | `gemini-1.5-flash` ya no existe. |
| `GEMINI_THINKING_LEVEL` | `low` | Deja tokens para la respuesta. |
| `LLM_MAX_TOKENS` | `1024` | Con 512 el razonamiento puede cortar la respuesta. |
| `CORS_ORIGINS` | `https://<dominio-de-la-web>` | Sin esto la API acepta cualquier origen. |
| `GEMINI_API_KEY` | (la clave) | Sin clave, asistente y resúmenes responden 503. |

Verificar: `curl https://<api>/health` → `{"status":"ok"}` y
`./scripts/smoke.sh https://<api>`.

## 3. Secrets de GitHub (Actions)

Settings → Secrets and variables → Actions:

- `GEMINI_API_KEY` (para "Probar LLM" y el resumen diario).
- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` (cron de
  retención y resumen diario).

## 4. Verificar el modelo de IA (sirve desde el celular)

GitHub → **Actions** → **Probar LLM** → **Run workflow**. Usa datos
ficticios. Debe terminar en verde con "RESULTADO: OK", latencia y "citas
válidas: 1 o más". Si Google retiró el modelo, probar otro nombre en el
campo `modelo` y luego cambiar `GEMINI_MODEL` en Render.

## 5. Web

Variables del hosting de la web (`.env.example` del repo web):
`VITE_API_URL`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`,
`VITE_POLICY_VERSION=2026-09-11` (debe coincidir con `POLICY_VERSION` de la
API o todo responde 409).

Verificar con una cuenta de prueba: registrar una entrada, corregirla con
motivo y ver "Editada"; pedir un resumen y abrir "Ver e imprimir"; preguntar
al asistente y abrir "Basado en N registros".

## 6. Antes del piloto con cuidadores reales (EDT 5.2.1)

**Decisión pendiente del equipo — capa gratuita de Gemini.** Sus términos
(vigentes desde el 23/03/2026) dicen que Google usa lo enviado para mejorar
sus productos, que revisores humanos pueden leerlo y piden no enviar datos
sensibles. La bitácora es dato de salud. Opciones:

- activar facturación en el proyecto de Gemini (capa pagada: no se usa para
  mejorar productos; para un piloto cuesta centavos de dólar), o
- usar Claude (`LLM_PROVIDER=claude` + `ANTHROPIC_API_KEY`), o
- usar la capa gratuita **solo con datos ficticios** (demo).

Ver `docs/REGISTRO_TRATAMIENTO.md` § Encargados.

## Si algo sale mal

- **El paquete SQL falla:** no se aplicó nada (transacción). Copiar el error
  y revisar; la base queda como estaba.
- **La API nueva falla después del paquete:** en Render, *Rollback* al deploy
  anterior **no sirve** (el código previo usa PATCH/DELETE de entradas, que
  ya no existen). Revisar logs de Render (sin contenido clínico) y la
  variable `GEMINI_MODEL`.
- **Asistente o resúmenes en 503 / "error":** casi siempre es la clave o el
  modelo. Paso 4.

---

## Rotación de claves (procedimiento)

Cuándo: si una clave se filtró (ver `docs/PLAN_BRECHAS.md`), si un
integrante con acceso deja el proyecto, o una vez por semestre.

| Clave | Dónde se rota | Dónde actualizarla después |
|---|---|---|
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Settings → API keys → rotar secret key | GitHub Secrets (nunca en Render ni en la web) |
| Clave pública (`anon` / publishable) | Supabase → Settings → API keys | Render (`SUPABASE_ANON_KEY`), hosting web (`VITE_SUPABASE_ANON_KEY`), app móvil |
| Llaves de firma JWT | Supabase → Settings → JWT keys (las nuevas son asimétricas; la API las lee del JWKS) | Si el proyecto aún usa HS256: Render (`SUPABASE_JWT_SECRET`). Todas las sesiones se cierran. |
| `GEMINI_API_KEY` | Google AI Studio → API keys → crear nueva y borrar la anterior | Render y GitHub Secrets |
| `ANTHROPIC_API_KEY` | Consola de Anthropic | Render |

Pasos:

1. Crear la clave nueva **antes** de borrar la anterior.
2. Actualizarla en cada lugar de la tabla y redeployar (Render: *Manual
   Deploy*; web: nuevo deploy).
3. Verificar: `scripts/smoke.sh`, "Probar LLM" y un login en la web.
4. Revocar la clave anterior.
5. Registrar la rotación (fecha, clave, motivo; nunca el valor) en el canal
   del equipo y, si fue por filtración, seguir `docs/PLAN_BRECHAS.md`.
