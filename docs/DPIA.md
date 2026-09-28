# Evaluación de Impacto en Protección de Datos (DPIA)

**App de apoyo a cuidadores · Grupo 7 · GPTI PUC 2026-2**
**Autor técnico:** David Boyd (backend). **Versión:** 2026-09-11.
**Marco:** Ley 21.719 (Chile). El tratamiento se realiza sobre categoría
especial (salud) con multiusuario y procesamiento por IA externa → DPIA
obligatoria en el criterio del art. 15 quater.

Este documento es una evaluación **simplificada** apropiada al scope de un
proyecto universitario. No sustituye una DPIA formal firmada por un DPO.

## 1. Contexto

- Producto: bitácora clínica compartida entre familiares cuidadores +
  asistente IA que responde sólo sobre lo registrado + resúmenes médicos
  generados.
- Volumen esperado: piloto de decenas a cientos de pacientes.
- Titulares: pacientes dependientes (niño/adulto mayor/enfermo crónico) →
  parte importante son **grupos vulnerables**.
- Datos: los descritos en `docs/REGISTRO_TRATAMIENTO.md`.

## 2. Necesidad y proporcionalidad

Cada categoría de dato registrada tiene una justificación funcional en el
`COMMENT ON COLUMN` del schema (`db/schema.sql`, sección "Minimización").
Ejemplo:
- `patients.fecha_nacimiento` es nullable — se recolecta sólo si el titular
  la aporta.
- `log_entries.content` guarda texto libre; el `kind` permite estadísticas
  sin leer contenido.
- Audio nunca llega al backend (transcripción en cliente).

## 3. Riesgos y mitigaciones

| # | Riesgo                                                                                                             | Prob. | Impacto | Mitigación de diseño                                                                                                                                                                       | Riesgo residual |
|---|---|---|---|---|---|
| R1 | Fuga de datos entre pacientes (usuario A ve datos de B).                                                          | Baja  | Alto    | RLS a nivel de fila con roles `admin/caregiver/viewer`; test automatizado (`tests/test_rls.py`) que rompe si una policy afloja. La API usa el JWT del usuario, no service role.             | Bajo. Depende de que RLS esté bien escrita — mitigado con tests + revisión en PR. |
| R2 | Autoría falsificada en bitácora (registro atribuido a otro cuidador).                                              | Baja  | Alto    | `author_id NOT NULL default auth.uid()` + `WITH CHECK (author_id = auth.uid())`. Test `test_author_id_no_puede_falsificarse` cubre el intento.                                             | Muy bajo. |
| R3 | El asistente IA inventa datos clínicos ("alucinación") que el cuidador cree.                                       | Media | Medio   | Contexto cerrado (grounding estricto): el LLM sólo ve la bitácora autorizada; se persiste `cited_entry_ids` para auditar. Prompt sistema explícito. **No hay function-calling.**            | Medio. **PENDIENTE JURÍDICO/CLÍNICO**: definir disclaimer visible en el cliente. |
| R4 | Proveedor LLM persiste o entrena con los datos que se le envían.                                                  | Media | Alto    | Enviamos contexto transitorio, no almacenamos en el proveedor. Proveedor abstraído (`app/llm.py`) para poder migrar. **PENDIENTE JURÍDICO**: contrato de encargo con Gemini/Anthropic.       | Medio hasta contrato firmado. |
| R5 | Acceso indebido de un miembro del grupo (curioseo).                                                                | Media | Medio   | Auditoría: cada lectura del feed que incluye entradas ajenas queda en `audit_log`. El admin puede consultar `GET /patients/{id}/audit`. Ver `docs/PLAN_BRECHAS.md`.                          | Bajo. |
| R6 | Contenido clínico filtrado a logs de aplicación o de auditoría.                                                    | Baja  | Alto    | `audit_log.metadata` sólo acepta counts/ids (regla escrita en `app/audit.py` y en el `COMMENT ON`). Logs de la app auditados: sólo hay uno en `errors.py` que no expone contenido.          | Bajo. |
| R7 | El titular pide sus datos y no podemos entregárselos (violación del derecho de acceso).                            | Baja  | Medio   | `GET /me/data-export` implementado y testeado. Exporta identidad, membresías, bitácora del autor, mensajes IA, pacientes creados.                                                          | Bajo. |
| R8 | El titular pide borrado y no podemos ejecutarlo sin intervención manual.                                           | Baja  | Alto    | `DELETE /me` con RPC `delete_my_account()` en la BD, y `DELETE /patients/{id}` con cascade. Ambos testeados (`tests/test_arco.py`).                                                        | Muy bajo. |
| R9 | Retención indefinida de datos derivados (mensajes IA).                                                             | Media | Bajo    | Cron mensual (`scripts/retention.py`) borra `assistant_messages > 6 meses`. Bitácora se conserva por diseño.                                                                                | Bajo. |
| R10 | Consentimiento desactualizado tras cambio de política.                                                            | Alta  | Bajo    | `require_consent` bloquea con 409 hasta que el usuario acepta la versión vigente. Aceptación datada e inmutable (`consents` append-only).                                                  | Muy bajo. |
| R11 | Un cuidador con acceso legítimo abusa (ej. imprime la bitácora y la comparte fuera del sistema).                  | Media | Medio   | No se puede prevenir técnicamente al 100%. Se mitiga con auditoría (`R5`) y con el contrato de uso (política de privacidad).                                                                | Medio (aceptado). |
| R12 | Compromiso de las claves de Supabase (URL/anon/service role).                                                     | Baja  | Alto    | `.env` fuera del repo (`.gitignore`); service role sólo en secrets de GitHub Actions, no en la API. Rotación manual documentada como pendiente operativo.                                  | Medio hasta contar con procedimiento formal de rotación. |
| R13 | Cold start de Render invalida sesiones o pierde requests durante demo.                                             | Alta  | Bajo    | Documentado en ARCHITECTURE.md §6. Lecturas críticas van directo a Supabase; no afecta a datos, sí a UX.                                                                                    | Bajo (aceptado). |

## 4. Riesgos residuales aceptados y por qué

- **R3 (alucinación IA)**: mitigado con grounding estricto y trazabilidad
  (`cited_entry_ids`) pero no eliminable. **Requiere disclaimer visible en
  el cliente** — trabajo del equipo de Móvil (María Paz).
- **R4 (contrato LLM)**: aceptado hasta que legal firme encargo. Migración
  a otro proveedor cuesta una función (`app/llm.py`).
- **R11 (abuso interno)**: aceptado con mitigación por auditoría; sin
  medida técnica adicional razonable para un piloto.

## 5. Consulta al titular

**PENDIENTE JURÍDICO**: la Ley 21.719 recomienda consultar al titular sobre
tratamientos de alto riesgo cuando sea razonable. Para categorías especiales
sobre grupos vulnerables (pacientes dependientes), evaluar si es factible
capturar el consentimiento del propio paciente (además del cuidador) o si
opera representación legal.

## 6. Conclusión

El tratamiento presenta un perfil de riesgo **medio**, dominado por la
sensibilidad del dato clínico y la participación de un LLM externo. Las
mitigaciones implementadas (RLS + auditoría + retención + contexto cerrado +
consentimiento versionado) son proporcionales al scope del proyecto
universitario. **La DPIA debe revisarse antes de cada release público** y
ante cualquier cambio en:

- proveedor LLM o modelo utilizado,
- estructura del schema que agregue categorías especiales,
- integración con nuevos encargados del tratamiento.
