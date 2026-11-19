-- Descripción: la vista log_entries_vigentes entrega también los datos de
-- trazabilidad de cada entrada. El resumen médico necesita una sola consulta
-- en vez de consultar revisiones y anulaciones por separado.
--
-- Idempotente. Las columnas nuevas van al final (CREATE OR REPLACE VIEW no
-- permite reordenar las existentes). Requiere PostgreSQL 15+.

create or replace view log_entries_vigentes with (security_invoker = true) as
select
  e.id,
  e.patient_id,
  e.author_id,
  e.source,
  e.registrado_en,
  e.created_at,
  coalesce(ed.kind, e.kind)               as kind,
  coalesce(ed.content, e.content)         as content,
  coalesce(ed.occurred_at, e.occurred_at) as occurred_at,
  (an.id is not null)                     as anulada,
  (ed.id is not null)                     as editada,
  coalesce(rv.revisiones_count, 0)        as revisiones_count,
  rv.ultima_revision_at                   as ultima_revision_at,
  rv.ultima_revision_autor_id             as ultima_revision_autor_id
from log_entries e
left join lateral (
  select r.id, r.kind, r.content, r.occurred_at
  from log_entry_revisions r
  where r.entry_id = e.id and r.accion = 'editar'
  order by r.created_at desc, r.id desc
  limit 1
) ed on true
left join lateral (
  select r.id
  from log_entry_revisions r
  where r.entry_id = e.id and r.accion = 'anular'
  limit 1
) an on true
left join lateral (
  select
    count(*) filter (where r.accion = 'editar')                      as revisiones_count,
    max(r.created_at) filter (where r.accion = 'editar')             as ultima_revision_at,
    (array_agg(r.author_id order by r.created_at desc, r.id desc)
       filter (where r.accion = 'editar'))[1]                        as ultima_revision_autor_id
  from log_entry_revisions r
  where r.entry_id = e.id
) rv on true;

grant select on log_entries_vigentes to authenticated;
