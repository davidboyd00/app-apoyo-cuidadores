# Migraciones de schema

**Regla desde hoy** (2026-09-11):

Cada cambio al modelo de datos se acompaña de **dos** archivos en el mismo PR:

1. **Delta**: nuevo archivo `db/migrations/YYYYMMDD_slug.sql` con **solo** los
   `ALTER TABLE` / `CREATE POLICY` / `DROP` necesarios para transformar la
   base productiva. Idempotente cuando sea posible (`IF NOT EXISTS`,
   `DROP POLICY IF EXISTS`).
2. **Foto completa**: actualizar `db/schema.sql` (sigue siendo la fuente de
   verdad para levantar de cero).

## Convenciones

- Formato de nombre: `YYYYMMDD_slug.sql` en minúsculas, kebab_case o
  snake_case. Ej.: `20261101_notificaciones_medicamentos.sql`.
- El archivo empieza con un comentario `-- Descripción: ...` (una línea).
- Cambios destructivos (`DROP COLUMN`, `DROP TABLE`) van en su propio PR con
  aviso previo al equipo (bloquea al front hasta actualizar tipos).
- Si el cambio agrega una categoría de datos personales/clínicos, actualizar
  también `docs/REGISTRO_TRATAMIENTO.md` en el mismo PR (Ley 21.719).

## Cómo aplicar en Supabase

Si hay varias pendientes, usar el paquete `db/APLICAR_EN_SUPABASE.sql`
(todas en orden, en una transacción, con tabla de verificación final). Se
arma con `python scripts/armar_paquete_migraciones.py`, actualizando ahí la
lista `PENDIENTES`. Si es una sola, se puede correr el archivo directo desde
el SQL Editor del proyecto. No hay tabla de tracking automática
por ahora (proyecto piloto); llevamos el registro por convención en la lista
de abajo.

## Registro de migraciones aplicadas

**23/11/2026:** las 5 pendientes y la semilla se aplicaron juntas con
`db/APLICAR_EN_SUPABASE.sql`, en una transacción y después de un ensayo
revertido. Verificación 11/11 y catálogo de producción idéntico a
`db/schema.sql` (columnas, restricciones, índices, policies, vista,
funciones, triggers, RLS y enums). Volver a correr el paquete no cambia nada.

**Para la próxima migración:** producción ya es igual al `db/schema.sql` de
hoy. Copiarlo como fixture nuevo (`tests/fixtures/schema_produccion_20261123.sql`),
apuntar `tests/test_migraciones.py` a ese archivo y dejar en `PENDIENTES` solo
las migraciones nuevas.

| Fecha       | Archivo | Estado    | Notas                                              |
|-------------|---------|-----------|----------------------------------------------------|
| (baseline)  | —       | aplicado  | `db/schema.sql` corrido en el proyecto Supabase.   |
| 2026-10-19  | `20261019_admin_cascade_realtime.sql` | aplicado  | Trigger de último admin + delete_my_account + log_entries en Realtime. Verificado (a)(b)(c). log_entries se agregó a mano a la publicación; el bloque DO se reescribió después para no depender del catálogo y avisar con NOTICE. |
| 2026-11-03  | `20261103_historial_inmutable_offline.sql` | aplicado 23/11 | Historial inmutable de la bitácora (`log_entry_revisions`, `log_entries_vigentes`; log_entries insert-only) + `client_id`/`registrado_en` para carga offline. Reemplaza PATCH/DELETE de entradas por revisiones y anulación. Requiere PostgreSQL 15+ (vista con `security_invoker`). Además corrige `delete_my_account`, que no reasignaba la autoría de `medication_doses`. |
| 2026-11-18  | `20261118_resumen_asincrono.sql` | aplicado 23/11 | Resúmenes en segundo plano: `summaries.estado` (`generando`/`listo`/`error`), `contenido` opcional, `author_id` y policy de actualización solo para el autor; publicación Realtime. |
| 2026-11-18  | `20261118_vista_trazabilidad.sql` | aplicado 23/11 | `log_entries_vigentes` agrega `revisiones_count`, `ultima_revision_at` y `ultima_revision_autor_id` para generar la nota de trazabilidad en una sola consulta. |
| 2026-11-21  | `20261121_asistente_estructurado.sql` | aplicado 23/11 | `assistant_messages.contexto_entry_ids` y `suficiente`; `cited_entry_ids` pasa a guardar solo las citas validadas. Los mensajes previos copian su contexto a la columna nueva. |
| 2026-11-21  | `20261121_turnos.sql` | aplicado 23/11 | Tabla `care_shifts` (turnos de cuidado) con helper `puede_estar_de_turno`, RLS y Realtime. |
| 2026-11-23  | `db/seed_placeholder.sql` (semilla) | aplicado 23/11 | Usuario anónimo `00000000-…` al que pasa la autoría al eliminar una cuenta (Ley 21.719). No existía en producción: `DELETE /me` fallaba por llave foránea para quien tenía registros. Con las columnas reales de Supabase Auth (tokens en `''`) para no romper la lista de usuarios del panel. |

Al aplicar una migración, agrega una fila con la fecha y el archivo.
