# Registro de actividades de tratamiento

**App de apoyo a cuidadores · Grupo 7 · GPTI PUC 2026-2**
**Responsable del tratamiento:** el proyecto (representante técnico: David Boyd, backend).
**Versión política vigente:** `2026-09-11` (ver `docs/POLITICA_PRIVACIDAD.md`).
**Marco:** Ley 21.719 (protección de datos personales, Chile), vigencia 01/12/2026.

Este registro está derivado del schema real (`db/schema.sql`) y del código de
la API (`app/`). Toda modificación al schema que agregue una nueva categoría
de dato debe actualizar este archivo en el mismo PR.

## Categorías tratadas

| Tabla / origen         | Categoría de dato                             | Sensibilidad          | Finalidad                                                                 |
|------------------------|-----------------------------------------------|-----------------------|---------------------------------------------------------------------------|
| `auth.users`           | Identidad del cuidador (email)                | Personal              | Autenticación (delegada a Supabase Auth).                                 |
| `patients`             | Identidad del paciente + notas clínicas       | **Sensible (salud)**  | Vincular el grupo de cuidado con el titular del dato.                     |
| `care_members`         | Membresía y rol en grupos de cuidado          | Personal              | Autorización (RLS): quién ve/edita qué.                                   |
| `log_entries`          | Bitácora clínica (síntomas, ánimo, notas)     | **Sensible (salud)**  | Registro compartido entre familiares + insumo del asistente IA.           |
| `medications`          | Régimen medicamentoso                         | **Sensible (salud)**  | Seguimiento de adherencia; futura base para recordatorios.                |
| `summaries`            | Resúmenes médicos generados                   | **Sensible (salud)**  | Documento derivado para consulta médica.                                  |
| `assistant_messages`   | Consultas al asistente IA + respuestas        | **Sensible (salud)**  | Historial del asistente; KPI de trazabilidad (`cited_entry_ids`).         |
| `consents`             | Aceptación versionada de la política          | Personal              | Evidencia del consentimiento (art. 12 Ley 21.719).                        |
| `audit_log`            | Registro de accesos y acciones sensibles      | Personal (metadatos)  | Evidencia operativa de cumplimiento; transparencia al admin del grupo.    |
| `centers`              | Datos públicos de centros de salud/farmacias  | No personal           | Consulta de referencia; poblada por scrapers.                             |

## Detalle por tabla

Cada bloque cubre las siete columnas del "registro" que exige la Ley 21.719
(campos derivados del art. 15).

### `patients`
- **Datos:** `nombre`, `fecha_nacimiento` (opcional), `notas` (opcional).
- **Base de licitud:** consentimiento del titular (o de su representante legal) manifestado por el admin del grupo al crear la ficha. **PENDIENTE JURÍDICO**: definir con legal si el consentimiento presunto del cuidador satisface la exigencia de consentimiento **explícito** para categoría especial, o si debe capturarse un acto adicional.
- **Dónde vive:** Supabase (PostgreSQL, tenant único del proyecto). Sin réplicas fuera del país.
- **Quién accede:** miembros del grupo de cuidado (RLS `patients_select`); solo el admin puede modificar/borrar. Verificado por tests de RLS.
- **Sale a terceros:** No.
- **Retención:** mientras exista el grupo. Al borrar el paciente (`DELETE /patients/{id}?confirmar=true`) cascadea todo su historial.
- **Derechos ejercitables:** acceso (`GET /me/data-export` incluye pacientes creados por el usuario); supresión (`DELETE /patients/{id}` solo admin, o `DELETE /me` para el creador).

### `log_entries` (bitácora)
- **Datos:** `content` (texto libre clínico), `kind` (enum), `occurred_at`, `source` (`text`/`voice`), `author_id`.
- **Base de licitud:** interés legítimo del grupo de cuidado y necesidad para prestación del servicio de acompañamiento. **PENDIENTE JURÍDICO**: confirmar con legal si esta base cubre el registro que hace un cuidador sobre el paciente, o si requiere consentimiento específico del titular.
- **Dónde vive:** Supabase. `content` **nunca** sale a logs de aplicación (regla dura documentada en el `COMMENT ON COLUMN`).
- **Quién accede:** miembros del grupo (RLS `entries_select`). Escritura solo admin/caregiver.
- **Sale a terceros:** Sí, al proveedor LLM como contexto **transitorio** al invocar `/assistant/ask` — el proveedor no persiste (ver §Terceros abajo).
- **Retención:** indefinida mientras exista el paciente. Se preserva incluso si el autor original se elimina (`author_id` reasignado al usuario placeholder — ver `db/schema.sql`).
- **Derechos:** acceso (`GET /me/data-export` incluye las entradas del propio autor); rectificación (`PATCH /entries/{id}` autor o admin); supresión (`DELETE /entries/{id}` autor o admin).

### `medications`
- **Datos:** `nombre`, `dosis`, `frecuencia_horas`, `horarios`, `notas`, `activo`.
- **Base de licitud:** misma que bitácora.
- **Dónde vive:** Supabase.
- **Quién accede:** miembros del grupo (RLS `meds_select`); escritura admin/caregiver.
- **Sale a terceros:** No directamente. Podrían aparecer en contexto de LLM si un caregiver los referencia en una entrada.
- **Retención:** desactivar (soft-delete) preserva el historial de adherencia. Borrado real vía cascade del paciente.
- **Derechos:** rectificación (`PATCH`); supresión (soft-delete o cascade).

### `summaries`
- **Datos:** `contenido` (texto), `desde`, `hasta`.
- **Base de licitud:** interés legítimo (documentación clínica derivada).
- **Dónde vive:** Supabase.
- **Quién accede:** miembros del grupo.
- **Sale a terceros:** al generarse pasan por el LLM (contexto transitorio); no se persiste allá.
- **Retención:** indefinida mientras exista el paciente. **PENDIENTE JURÍDICO**: definir plazo con legal — propuesta razonable es "vida del paciente en la plataforma".
- **Derechos:** acceso, supresión vía cascade.

### `assistant_messages`
- **Datos:** `pregunta`, `respuesta`, `cited_entry_ids`, `author_id`.
- **Base de licitud:** consentimiento (al aceptar la política, el cuidador consiente el uso del asistente IA).
- **Dónde vive:** Supabase.
- **Quién accede:** miembros del grupo (RLS `asst_select`).
- **Sale a terceros:** cada consulta envía `pregunta` + contexto de bitácora al LLM. Ver §Terceros.
- **Retención:** **6 meses** (configurable con `RETENTION_MONTHS_ASSISTANT`). Purga automática mensual vía `scripts/retention.py` + `.github/workflows/retention.yml`. Justificación: dato derivado que pierde valor operativo pasado el semestre; conservarlo indefinidamente aumenta superficie sin beneficio.
- **Derechos:** acceso (`GET /me/data-export` incluye mensajes propios); supresión vía cascade del paciente o vencimiento por retención.

### `consents`
- **Datos:** `user_id`, `version`, `aceptado_en`.
- **Base de licitud:** obligación legal (art. 12 Ley 21.719 exige evidencia).
- **Dónde vive:** Supabase.
- **Quién accede:** solo el propio usuario (RLS `consents_select`). Escritura solo por el propio user (`POST /me/consent`).
- **Sale a terceros:** No.
- **Retención:** indefinida (append-only, sin policies `UPDATE`/`DELETE`). Es la evidencia del consentimiento.
- **Derechos:** acceso (`GET /me/consent`); no aplica rectificación (append-only); supresión vía cascade al eliminar la cuenta.

### `audit_log`
- **Datos:** `user_id`, `accion`, `recurso_tipo`, `recurso_id`, `patient_id`, `metadata` (jsonb con counts/flags), `created_at`.
- **Base de licitud:** obligación legal + interés legítimo (evidencia de cumplimiento y transparencia al titular).
- **Dónde vive:** Supabase.
- **Quién accede:** admin del grupo ve las acciones sobre el paciente (RLS `audit_read`); el propio usuario ve sus acciones sin `patient_id`.
- **Sale a terceros:** No.
- **Retención:** **PENDIENTE JURÍDICO**. Propuesta técnica: 2 años (permite responder a un control regulatorio típico). Si legal define otro plazo, se agrega al `scripts/retention.py`.
- **Derechos:** acceso al propio historial vía `GET /me/data-export` (**PENDIENTE**: sumar `audit_log` propio al export si legal lo exige). El admin puede consultar vía `GET /patients/{id}/audit`.

### `care_members`
- **Datos:** `patient_id`, `user_id`, `role`.
- **Base de licitud:** ejecución del servicio.
- **Dónde vive:** Supabase.
- **Quién accede:** miembros del grupo (RLS `care_members_select`); gestión admin.
- **Retención:** vida del grupo. Cascade al borrar paciente o al `delete_my_account`.
- **Derechos:** acceso vía `GET /me/data-export`; supresión al eliminar la cuenta o el paciente.

### `centers`
- No hay dato personal. Datos públicos poblados por scrapers (con service role, sin RLS aplicada para lectura anónima).

## Encargados del tratamiento (terceros)

| Tercero               | Rol                                          | Qué recibe                                                                          | Persiste allá         |
|-----------------------|----------------------------------------------|-------------------------------------------------------------------------------------|-----------------------|
| Supabase (Postgres/Auth/Realtime) | Encargado principal (base de datos + auth)    | Todo el tratamiento (es el hosting).                                                | Sí (es el repositorio).|
| Render (hosting API)  | Encargado (compute)                          | Requests en tránsito; no persiste datos clínicos (la API es stateless).             | No (efímero).         |
| Gemini / Anthropic (LLM) | Encargado ad-hoc (procesamiento IA transitorio) | Por consulta: pregunta del cuidador + contexto de bitácora del paciente autorizado. | **No** (no almacenamos en el proveedor; contexto transitorio). |
| GitHub Actions        | Encargado (ejecución de cron y CI)           | Corre `scripts/retention.py` con service role.                                      | No (efímero por ejecución). |

**PENDIENTE JURÍDICO**: firmar contrato de encargo (art. 24 Ley 21.719) con
Supabase, Render y el proveedor LLM elegido, o documentar por qué las
cláusulas de servicio vigentes lo satisfacen.

## Transferencias internacionales

Supabase y los proveedores LLM tienen infraestructura fuera de Chile. **PENDIENTE JURÍDICO**: evaluar con legal si se requieren garantías adicionales (art. 27 y siguientes Ley 21.719).

## Actualización

Este documento se revisa en cada cambio a `db/schema.sql` que agregue o
elimine una tabla / columna con datos personales.
