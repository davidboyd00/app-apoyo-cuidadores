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

Desde el SQL Editor del proyecto, ejecutar el archivo de migración más
reciente que aún no se haya aplicado. No hay tabla de tracking automática
por ahora (proyecto piloto); llevamos el registro por convención en la lista
de abajo.

## Registro de migraciones aplicadas

| Fecha       | Archivo | Estado    | Notas                                              |
|-------------|---------|-----------|----------------------------------------------------|
| (baseline)  | —       | aplicado  | `db/schema.sql` corrido en el proyecto Supabase.   |

Al aplicar una migración, agrega una fila con la fecha y el archivo.
