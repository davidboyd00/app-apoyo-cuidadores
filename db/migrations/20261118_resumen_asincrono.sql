-- Descripción: resúmenes médicos asíncronos. El resumen se crea en estado
-- 'generando' y se completa en segundo plano ('listo') o falla ('error').
--
-- Idempotente: se puede correr más de una vez.
-- Efecto en la API: POST /patients/{id}/summaries responde 202 con el resumen
-- en 'generando'. La app recibe el cambio por Realtime o consultando GET.
-- Si existe un resumen 'listo' de las últimas 24 h, POST lo devuelve sin
-- llamar al LLM (salvo que se pida forzar=true).

-- 1) Estado del resumen. Los existentes ya tienen contenido: quedan 'listo'.
alter table summaries add column if not exists estado text not null default 'listo';
alter table summaries drop constraint if exists summaries_estado_ck;
alter table summaries add constraint summaries_estado_ck
  check (estado in ('generando', 'listo', 'error'));

-- 2) El contenido solo es obligatorio cuando el resumen está listo.
alter table summaries alter column contenido drop not null;
alter table summaries drop constraint if exists summaries_contenido_listo_ck;
alter table summaries add constraint summaries_contenido_listo_ck
  check (estado <> 'listo' or contenido is not null);

-- 3) Autor: quien pidió el resumen. Solo él puede completarlo o marcarlo con
--    error (la tarea en segundo plano corre con su sesión). Si la cuenta se
--    elimina, el resumen queda sin autor en vez de bloquear el borrado.
alter table summaries add column if not exists author_id uuid
  references auth.users(id) on delete set null;
alter table summaries alter column author_id set default auth.uid();

-- 4) Policy de actualización: solo el autor, y solo admin/caregiver.
drop policy if exists summaries_update on summaries;
create policy summaries_update on summaries
  for update
  using (author_id = auth.uid() and (has_role(patient_id, 'admin') or has_role(patient_id, 'caregiver')))
  with check (author_id = auth.uid() and (has_role(patient_id, 'admin') or has_role(patient_id, 'caregiver')));

-- 5) Realtime: la app se entera cuando el resumen pasa a 'listo' o 'error'.
do $$
begin
  alter publication supabase_realtime add table public.summaries;
  raise notice 'Realtime: summaries agregada a supabase_realtime.';
exception
  when duplicate_object then
    raise notice 'Realtime: summaries ya estaba en supabase_realtime (sin cambios).';
  when undefined_object then
    raise notice 'Realtime: no existe la publicación supabase_realtime (fuera de Supabase); se omite.';
end
$$;
