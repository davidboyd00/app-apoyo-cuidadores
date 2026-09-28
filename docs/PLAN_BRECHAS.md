# Plan de respuesta ante vulneraciones de datos

**App de apoyo a cuidadores · Grupo 7 · GPTI PUC 2026-2**
**Marco:** Ley 21.719, arts. 43-45 (obligación de notificar).
**Plazo legal para notificar a la Agencia:** **72 horas** desde que se toma
conocimiento razonable de la vulneración.

Este plan es operativo y minimalista, apropiado al scope de un piloto
universitario. Al pasar a producción real debe formalizarse con roles y
canales de comunicación reales.

## 1. Qué cuenta como vulneración

Cualquier incidente que comprometa la **confidencialidad, integridad o
disponibilidad** de datos personales, incluyendo (no exhaustivo):

- Filtración de bitácora / medicamentos entre grupos de cuidado.
- Acceso indebido de un usuario a datos de un paciente del que no es miembro.
- Publicación accidental de secretos (`SUPABASE_SERVICE_ROLE_KEY`, `.env`, JWT).
- Compromiso de la cuenta Supabase o del hosting.
- Fuga a través del proveedor LLM (respuestas que contengan datos de otro paciente).
- Borrado o corrupción masiva no autorizada.

## 2. Cómo se detecta

### 2.1 Detección automática

Estas señales deberían disparar revisión inmediata:

- CI de tests de RLS **rojo** en `main` (`.github/workflows/ci.yml`). Bloqueo
  automático de merges — la política de RLS habría dejado de proteger.
- Fallas repetidas del RPC `audit_write` (visible como huecos en `audit_log`
  cruzados con logs de la app).
- Errores 500 con SQLSTATE `42501` (permission denied) en volumen anómalo →
  posible intento de saltarse RLS.

### 2.2 Detección por revisión (checklist manual, semanal)

Consultas sobre `audit_log` que **el admin del grupo puede ejecutar**
(RLS le muestra sólo su paciente); las consultas globales requieren service
role y las ejecuta el backend dev.

**A. Accesos ajenos anómalos por paciente** (admin del grupo):
```sql
select user_id, count(*) as veces
from audit_log
where patient_id = <PID>
  and accion = 'read_entries_ajenas'
  and created_at > now() - interval '7 days'
group by user_id
order by veces desc;
```
Alerta si algún usuario tiene un volumen desproporcionado al resto.

**B. Consultas IA con contexto vacío** (global, backend dev con service role):
```sql
select id, user_id, patient_id, metadata
from audit_log
where accion = 'assistant_query'
  and (metadata->>'cited_count')::int = 0;
```
Puede indicar intentos de extraer respuestas sin contexto legítimo.

**C. Borrados de paciente o cuenta** (global):
```sql
select created_at, user_id, accion, recurso_id
from audit_log
where accion in ('delete_patient', 'self_delete')
order by created_at desc
limit 50;
```
Cualquier volumen inusual dispara alerta.

**D. Consentimientos aceptando versiones antiguas** (no debería suceder — la
API lo rechaza con 409, pero si aparece en el log es indicio de bug o abuso
del RPC):
```sql
select user_id, metadata->>'version' as version, created_at
from audit_log
where accion = 'consent_accept'
  and metadata->>'version' <> current_setting('app.policy_version', true);
```

## 3. Procedimiento de respuesta (72 horas)

**T+0 — Detección.** Quien detecta abre el canal del equipo y captura:
   - Qué señal disparó la alerta.
   - Timestamp y usuarios/pacientes involucrados (ids, sin contenido).
   - Alcance estimado.

**T+2h — Contención.** El backend dev:
   - Aplica hotfix si es un bug en RLS o en la API (rollback si es más rápido).
   - Rota claves comprometidas (Supabase, si aplica).
   - Deshabilita la cuenta del atacante si es un usuario interno (via SQL
     directo, con service role, hasta tener endpoint dedicado).

**T+24h — Evaluación de impacto.** Reunir:
   - Categorías de datos afectadas (ver `docs/REGISTRO_TRATAMIENTO.md`).
   - N° de titulares afectados (query sobre `audit_log`).
   - Origen probable (código, config, proveedor, humano).

**T+48h — Preparación de notificación.** Redactar:
   - **A la Agencia de Protección de Datos**: descripción, categorías,
     titulares afectados, consecuencias, medidas adoptadas y planeadas.
   - **A los titulares** (cuando el riesgo sea alto para sus derechos):
     comunicación clara y directa vía email registrado en `auth.users`.

**T+72h — Notificación formal.** Enviar dentro del plazo legal.

**T+7 días — Post-mortem.** Documentar en el repo (nuevo archivo bajo
`docs/incidentes/YYYYMMDD-<slug>.md`) qué pasó, por qué, y qué medida
permanente se implementó (test nuevo, policy nueva, cambio en `retention.py`,
etc.). Sin datos personales en el post-mortem.

## 4. Roles

| Rol                         | Responsabilidad                                                                                | Persona                       |
|-----------------------------|-----------------------------------------------------------------------------------------------|-------------------------------|
| Backend / detección técnica | Correr queries, aplicar hotfix, rotar claves, redactar informe técnico.                       | David Boyd                    |
| Móvil                       | Bajar mensajes de disclaimer / aviso en el cliente si es necesario.                          | María Paz                     |
| Coordinación / notificación | Contactar Agencia y titulares.                                                                | **PENDIENTE JURÍDICO** (¿profesor guía? ¿coordinador Grupo 7?) |
| DPO / asesoría legal        | Interpretación de la ley, redacción legal de la notificación.                                  | **PENDIENTE JURÍDICO** (proyecto universitario sin DPO formal) |

## 5. Ejercicios (drills)

Antes del release público, ejecutar al menos un **drill** simulado:

1. Ejercicio A: pedirle al backend dev que rompa una policy en un branch,
   verificar que los tests de RLS lo cachan y que el proceso de rollback
   funciona.
2. Ejercicio B: simular un post-mortem sobre un incidente ficticio y
   cronometrar cuánto tarda el equipo en producir el borrador de
   notificación (debe ser < 24 h).

## 6. Actualización

Este plan se revisa:
- Al cambiar el schema de `audit_log`.
- Al cambiar el proveedor LLM.
- Al pasar de piloto a producción (redefinir roles con nombres reales).
