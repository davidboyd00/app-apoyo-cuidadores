# Puesta en producción del MVP (checklist)

Pasos que requieren acceso a Supabase, Render o GitHub y que no se pueden
hacer desde el código. En orden. Cada paso dice cómo verificarlo.

> **Importante:** Render despliega `main` automáticamente (`render.yaml`,
> `autoDeploy: true`). La API de noviembre necesita el paso 1, que **ya está
> aplicado (23/11)**. El paso 2 y la web (paso 5) también quedaron listos el
> 23/11; los pasos 3 y 4 requieren acceso a GitHub.

## 1. Base de datos (Supabase) · ✅ hecho el 23/11/2026

Aplicado después de un ensayo revertido: verificación 11/11 y catálogo de
producción idéntico a `db/schema.sql`. Incluye la semilla del usuario
anónimo, que faltaba (sin ella, eliminar una cuenta con registros fallaba).

Chequeos de seguridad y rendimiento de Supabase (panel → Advisors), 23/11:

- **Seguridad, sin hallazgos críticos.** Avisos aceptados:
  - Las funciones SECURITY DEFINER se pueden llamar con la clave pública
    (`anon`). Sin sesión no hacen nada, porque validan `auth.uid()`.
    Endurecimiento sugerido: `revoke execute ... from anon`.
  - "Leaked password protection" requiere plan Pro.
- **Rendimiento:** índices sin uso, llaves foráneas sin índice y
  `auth.uid()` por fila en las policies. Con el volumen del piloto no importa.

Los pasos de abajo quedan para un proyecto nuevo.

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

**✅ Hecho el 23/11/2026.** Servicio `caregivers-backend` creado con el
Blueprint (`render.yaml`, plan gratuito, Oregon):
**https://caregivers-backend.onrender.com**. `CORS_ORIGINS` apunta a la web
(paso 5). El plan gratuito se duerme tras 15 minutos sin uso: la primera
petición después tarda cerca de un minuto (abrirla antes de una demo).

**Despliegue de cada cambio:** hoy Render **no** despliega solo al hacer push
(los Blueprints se crearon desde la URL pública del repo, sin la app de GitHub
de Render). Para que lo haga: Render → Account Settings → GitHub → dar acceso a
`app-apoyo-cuidadores` y `app-apoyo-cuidadores-web`. Mientras tanto, cada
cambio se despliega a mano (servicio → **Manual Deploy**).

El resumen médico usa su propio límite de tokens (`LLM_MAX_TOKENS_RESUMEN`,
4096 por defecto; el asistente usa `LLM_MAX_TOKENS`). Si el modelo corta por
el límite, el resumen queda en "error" (nunca se guarda un texto a medias).

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

**Prueba completa con video:** Actions → **Video del asistente (IA real)** →
Run workflow. Levanta la app con una familia ficticia y el Gemini real, graba
en formato celular dos preguntas al asistente y un resumen, y deja el video y
`resultado.json` como artefacto (y en la rama `videos`). Tarda ~4 minutos.
Primera corrida (23/11): respuestas con citas válidas, "la bitácora no
alcanza" cuando corresponde y resumen completo; latencia de 2 a 37 s según la
carga de la capa gratuita.

**Prueba rápida:**

GitHub → **Actions** → **Probar LLM** → **Run workflow**. Usa datos
ficticios. Debe terminar en verde con "RESULTADO: OK", latencia y "citas
válidas: 1 o más". Si Google retiró el modelo, probar otro nombre en el
campo `modelo` y luego cambiar `GEMINI_MODEL` en Render.

## 5. Web

**✅ Hecho el 23/11/2026.** Sitio estático `caregivers-web` creado con el
`render.yaml` del repo web: **https://caregivers-web-wxm3.onrender.com**.
Render le agregó `-wxm3` porque `caregivers-web.onrender.com` es de otra
cuenta: no usar esa dirección. Esa URL va en `CORS_ORIGINS` de la API y en
Supabase → Authentication → URL Configuration → Site URL.

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
- **Nada responde y Supabase dice "Paused":** el plan gratuito pausa el
  proyecto tras una semana sin actividad. En el panel, *Restore project*
  (tarda unos minutos, no se pierden datos). Revisarlo antes de cada demo.

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
