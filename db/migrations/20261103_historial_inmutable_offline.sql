-- Descripción: historial inmutable de la bitácora (revisiones y anulaciones sin hard delete) + idempotencia y hora de registro para carga offline.
--
-- Idempotente: se puede correr más de una vez.
-- Efecto en la API: PATCH y DELETE de /patients/{id}/entries/{entry_id} dejan
-- de existir. Se reemplazan por POST .../revisiones (editar) y POST .../anulacion.
--
-- Decisiones (ver docs/REGISTRO_TRATAMIENTO.md § log_entries):
--   * log_entries pasa a ser insert-only: ni autor ni admin la modifican ni la borran.
--   * Cada corrección es una fila en log_entry_revisions (append-only) con autor,
--     hora y motivo obligatorio. La entrada original nunca se pierde.
--   * La vista log_entries_vigentes entrega el estado actual de cada entrada.
--   * Las entradas offline llevan client_id (idempotencia ante reintentos) y
--     registrado_en (hora en el dispositivo); created_at queda como hora de sincronización.

-- 1) Columnas de sincronización offline -------------------------------------
alter table log_entries      add column if not exists client_id     uuid;
alter table log_entries      add column if not exists registrado_en timestamptz;
alter table medication_doses add column if not exists client_id     uuid;
alter table medication_doses add column if not exists registrado_en timestamptz;

-- Un mismo cliente no puede enviar dos veces el mismo registro: el reintento
-- de una cola offline devuelve la fila existente en vez de duplicarla.
create unique index if not exists log_entries_author_client_uk
  on log_entries (author_id, client_id) where client_id is not null;
create unique index if not exists medication_doses_author_client_uk
  on medication_doses (author_id, client_id) where client_id is not null;

-- 2) Revisiones (append-only) -------------------------------------------------
create table if not exists log_entry_revisions (
  id           uuid primary key default gen_random_uuid(),
  entry_id     uuid not null references log_entries(id) on delete cascade,
  patient_id   uuid not null references patients(id) on delete cascade,
  author_id    uuid not null default auth.uid() references auth.users(id) on delete restrict,
  accion       text not null check (accion in ('editar', 'anular')),
  motivo       text not null check (char_length(btrim(motivo)) between 3 and 500),
  -- Estado nuevo completo (solo en 'editar'). En 'anular' quedan en null.
  kind         entry_kind,
  content      text,
  occurred_at  timestamptz,
  created_at   timestamptz not null default clock_timestamp(),
  check (
    (accion = 'editar' and kind is not null and occurred_at is not null
       and content is not null and char_length(btrim(content)) > 0) or
    (accion = 'anular' and kind is null and occurred_at is null and content is null)
  )
);
-- Una sola anulación por entrada: anular es terminal.
create unique index if not exists log_entry_revisions_una_anulacion_uk
  on log_entry_revisions (entry_id) where accion = 'anular';
create index if not exists log_entry_revisions_entry_idx
  on log_entry_revisions (entry_id, created_at desc);
create index if not exists log_entry_revisions_patient_idx
  on log_entry_revisions (patient_id, created_at desc);

-- Estado vigente de cada entrada. security_invoker = true: la vista aplica la
-- RLS del usuario que consulta (miembros ven el historial; nadie ve filas ajenas).
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
  (ed.id is not null)                     as editada
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
) an on true;

-- 3) Helpers SECURITY DEFINER (regla sagrada #0: nada de EXISTS inline) -----
create or replace function entrada_anulada(eid uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from log_entry_revisions
    where entry_id = eid and accion = 'anular'
  );
$$;

-- Quién puede revisar/anular: el autor original o un admin del grupo, y la
-- entrada debe pertenecer al paciente indicado (evita revisiones cruzadas).
create or replace function puede_revisar_entrada(eid uuid, pid uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from log_entries e
    where e.id = eid
      and e.patient_id = pid
      and (e.author_id = auth.uid() or has_role(pid, 'admin'))
  );
$$;

-- 4) RLS ----------------------------------------------------------------------
alter table log_entry_revisions enable row level security;

-- log_entries: se eliminan UPDATE y DELETE. Sin policy, RLS las bloquea para todos.
drop policy if exists entries_update on log_entries;
drop policy if exists entries_delete on log_entries;

-- Lectura del historial: cualquier miembro del grupo.
drop policy if exists revisiones_select on log_entry_revisions;
create policy revisiones_select on log_entry_revisions
  for select using (is_member(patient_id));

-- Escritura: admin o caregiver, como autor de la revisión, sobre una entrada
-- que puede revisar y que aún no está anulada. Sin policies UPDATE/DELETE.
drop policy if exists revisiones_insert on log_entry_revisions;
create policy revisiones_insert on log_entry_revisions
  for insert with check (
    author_id = auth.uid()
    and (has_role(patient_id, 'admin') or has_role(patient_id, 'caregiver'))
    and puede_revisar_entrada(entry_id, patient_id)
    and not entrada_anulada(entry_id)
  );

-- 5) Grants (Supabase ya los da por defecto; se explicitan para el mock local).
grant select, insert on log_entry_revisions to authenticated;
grant select on log_entries_vigentes to authenticated;

-- 6) DELETE /me: la autoría de las revisiones también se anonimiza (FK restrict).
--    Se reescribe la función completa con esa línea agregada.
create or replace function delete_my_account()
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  me uuid := auth.uid();
  placeholder constant uuid := '00000000-0000-0000-0000-000000000000';
begin
  if me is null then
    raise exception 'no autenticado' using errcode = '28000';
  end if;
  if me = placeholder then
    raise exception 'no permitido' using errcode = '42501';
  end if;

  -- Anonimiza autoría en registros que se preservan.
  update log_entries          set author_id = placeholder where author_id = me;
  update log_entry_revisions  set author_id = placeholder where author_id = me;
  update medication_doses     set author_id = placeholder where author_id = me;
  update assistant_messages   set author_id = placeholder where author_id = me;

  -- Pacientes de los que es el único miembro: quedarían huérfanos → se
  -- eliminan (nadie más puede acceder ni ejercer derechos sobre ellos, y su
  -- historial no debe quedar en el vacío). Va ANTES de salir de los grupos:
  -- así su membresía se borra por la cascada del paciente, que el trigger
  -- de último admin permite; borrarla directo sería "quitar al único admin".
  delete from patients p
  where exists (select 1 from care_members cm where cm.patient_id = p.id and cm.user_id = me)
    and not exists (select 1 from care_members cm where cm.patient_id = p.id and cm.user_id <> me);

  -- Sale del resto de los grupos. Si es el único admin de un grupo con más
  -- miembros, el trigger lo impide: debe asignar otro admin antes.
  delete from care_members where user_id = me;

  -- Finalmente elimina la cuenta de auth (Supabase la maneja como cualquier
  -- fila de auth.users; en el mock local también).
  delete from auth.users where id = me;
end;
$$;

revoke all on function delete_my_account() from public;
grant execute on function delete_my_account() to authenticated;

-- 7) Realtime: el feed debe enterarse de revisiones y anulaciones, no solo de
--    inserts. Se agrega la tabla de revisiones a la publicación.
do $$
begin
  alter publication supabase_realtime add table public.log_entry_revisions;
  raise notice 'Realtime: log_entry_revisions agregada a supabase_realtime.';
exception
  when duplicate_object then
    raise notice 'Realtime: log_entry_revisions ya estaba en supabase_realtime (sin cambios).';
  when undefined_object then
    raise notice 'Realtime: no existe la publicación supabase_realtime (fuera de Supabase); se omite.';
end
$$;

-- 8) Verificación (solo lectura). Esperado:
--    a) log_entries sin policies UPDATE/DELETE (cero filas).
select policyname from pg_policies
where schemaname = 'public' and tablename = 'log_entries'
  and cmd in ('UPDATE', 'DELETE');
--    b) dos filas: revisiones con RLS habilitada y la vista creada.
select relname, relrowsecurity from pg_class
where relname = 'log_entry_revisions' and relnamespace = 'public'::regnamespace;
