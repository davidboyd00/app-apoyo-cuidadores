-- ============================================================================
-- PAQUETE DE MIGRACIONES PENDIENTES · aplicar UNA vez en Supabase
-- Generado por scripts/armar_paquete_migraciones.py (no editar a mano).
--
-- Incluye, en orden:
--   1. 20261103_historial_inmutable_offline.sql
--   2. 20261118_resumen_asincrono.sql
--   3. 20261118_vista_trazabilidad.sql
--   4. 20261121_asistente_estructurado.sql
--   5. 20261121_turnos.sql
--
-- Cómo aplicarlo (2 minutos):
--   1. Supabase → SQL Editor → New query.
--   2. Pegar este archivo COMPLETO y presionar Run.
--   3. Corre en una sola transacción: si algo falla, no se aplica nada y
--      la base queda como estaba.
--   4. Al final aparece una tabla `verificacion`: todas las filas deben
--      decir ok = true.
--   5. Marcar las migraciones como "aplicado" en db/migrations/README.md.
--
-- Es idempotente: correrlo dos veces no rompe nada. Requiere PostgreSQL 15+.
-- ============================================================================

begin;

-- >>>>>>>>>> 1/5 · 20261103_historial_inmutable_offline.sql

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
-- drop + create (no solo `create or replace`): si el paquete se corre de nuevo
-- después de 20261118_vista_trazabilidad, la vista ya tiene más columnas y
-- Postgres no permite quitarlas con `create or replace`. El grant se repite abajo.
drop view if exists log_entries_vigentes;
create view log_entries_vigentes with (security_invoker = true) as
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

-- >>>>>>>>>> 2/5 · 20261118_resumen_asincrono.sql

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

-- >>>>>>>>>> 3/5 · 20261118_vista_trazabilidad.sql

-- Descripción: la vista log_entries_vigentes entrega también los datos de
-- trazabilidad de cada entrada. El resumen médico necesita una sola consulta
-- en vez de consultar revisiones y anulaciones por separado.
--
-- Idempotente. Las columnas nuevas van al final (CREATE OR REPLACE VIEW no
-- permite reordenar las existentes). Requiere PostgreSQL 15+.

-- drop + create: deja la vista exactamente con estas columnas aunque se corra
-- varias veces o en otro orden. El grant se repite abajo.
drop view if exists log_entries_vigentes;
create view log_entries_vigentes with (security_invoker = true) as
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

-- >>>>>>>>>> 4/5 · 20261121_asistente_estructurado.sql

-- Descripción: asistente con salida estructurada: citas validadas, contexto visto y suficiencia.
--
-- Idempotente: se puede correr más de una vez.
-- Efecto en la API: POST /assistant/ask guarda en `cited_entry_ids` solo las
-- entradas que la respuesta cita (validadas contra el contexto), en
-- `contexto_entry_ids` todas las que vio el modelo y en `suficiente` si la
-- bitácora alcanzaba. Nuevo GET /metrics/asistente (KPI >90% verificable).

alter table assistant_messages add column if not exists contexto_entry_ids uuid[] not null default '{}';
alter table assistant_messages add column if not exists suficiente boolean;

-- Mensajes anteriores: lo que se guardaba en cited_entry_ids era el contexto completo.
update assistant_messages
   set contexto_entry_ids = cited_entry_ids
 where contexto_entry_ids = '{}' and cited_entry_ids <> '{}';

comment on column assistant_messages.cited_entry_ids is 'Entradas en que se basa la respuesta (citas del modelo validadas contra el contexto). Regla sagrada #4 · KPI >90% verificable.';
comment on column assistant_messages.contexto_entry_ids is 'Todas las entradas que vio el modelo como contexto. Regla sagrada #4 (auditoría de lo visto).';
comment on column assistant_messages.suficiente is 'False si el asistente declaró que la bitácora no alcanzaba para responder. Null en mensajes previos a la salida estructurada.';

-- >>>>>>>>>> 5/5 · 20261121_turnos.sql

-- Descripción: turnos de cuidado (care_shifts) para el relevo familiar, con RLS y Realtime.
--
-- Idempotente: se puede correr más de una vez.
-- Efecto en la API: GET/POST /patients/{id}/turnos y DELETE /patients/{id}/turnos/{turno_id}.
-- Ley 21.719: categoría nueva de dato personal (coordinación del cuidado, sin
-- contenido clínico); ver docs/REGISTRO_TRATAMIENTO.md.

-- ============================================================================
-- Turnos de cuidado (relevo familiar · EDT 2.1.1 / 2.2.2)
-- Quién está a cargo y cuándo. NO guarda contenido clínico: el resumen del
-- turno se arma con las entradas de la bitácora dentro de su ventana.
-- ============================================================================
create table if not exists care_shifts (
  id           uuid primary key default gen_random_uuid(),
  patient_id   uuid not null references patients(id) on delete cascade,
  -- Persona a cargo. Si elimina su cuenta, sus turnos se eliminan con ella.
  cuidador_id  uuid not null references auth.users(id) on delete cascade,
  inicio       timestamptz not null,
  fin          timestamptz not null,
  creado_por   uuid default auth.uid() references auth.users(id) on delete set null,
  created_at   timestamptz not null default now(),
  constraint care_shifts_rango_ck check (fin > inicio and fin - inicio <= interval '7 days')
);

create index if not exists care_shifts_patient_inicio_idx on care_shifts (patient_id, inicio);
create index if not exists care_shifts_cuidador_idx on care_shifts (cuidador_id);

alter table care_shifts enable row level security;
grant select, insert, delete on care_shifts to authenticated;

-- ¿`uid` puede estar a cargo de `pid`? Solo admin o caregiver del grupo
-- (un viewer no cuida). SECURITY DEFINER por la regla sagrada #0: consulta
-- care_members sin pasar por su propia RLS.
create or replace function puede_estar_de_turno(pid uuid, uid uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from care_members
    where patient_id = pid and user_id = uid and role in ('admin', 'caregiver')
  );
$$;

drop policy if exists turnos_select on care_shifts;
create policy turnos_select on care_shifts
  for select using (is_member(patient_id));

-- Admin agenda a cualquier admin/caregiver del grupo; caregiver solo a sí mismo.
drop policy if exists turnos_insert on care_shifts;
create policy turnos_insert on care_shifts
  for insert with check (
    creado_por = auth.uid()
    and puede_estar_de_turno(patient_id, cuidador_id)
    and (
      has_role(patient_id, 'admin')
      or (has_role(patient_id, 'caregiver') and cuidador_id = auth.uid())
    )
  );

-- Cancelar: quien lo creó, la persona a cargo o un admin (siempre miembros).
drop policy if exists turnos_delete on care_shifts;
create policy turnos_delete on care_shifts
  for delete using (
    is_member(patient_id)
    and (creado_por = auth.uid() or cuidador_id = auth.uid() or has_role(patient_id, 'admin'))
  );

comment on table  care_shifts            is 'Turnos de cuidado (relevo familiar). Sin contenido clínico. Ley 21.719: dato personal de coordinación (quién cuida y cuándo).';
comment on column care_shifts.cuidador_id is 'Persona a cargo durante el turno. Cascada al eliminar la cuenta (no es evidencia clínica).';
comment on column care_shifts.creado_por  is 'Quién agendó el turno. Null si esa cuenta se eliminó.';

-- Los turnos también se propagan en vivo (quién está a cargo ahora).
do $$
begin
  alter publication supabase_realtime add table public.care_shifts;
  raise notice 'Realtime: care_shifts agregada a supabase_realtime.';
exception
  when duplicate_object then
    raise notice 'Realtime: care_shifts ya estaba en supabase_realtime (sin cambios).';
  when undefined_object then
    raise notice 'Realtime: no existe la publicación supabase_realtime (fuera de Supabase); se omite.';
end
$$;

commit;

-- Verificación (solo lectura). Todas las filas deben decir ok = true.
select item, ok from (values
  ('tabla log_entry_revisions', to_regclass('public.log_entry_revisions') is not null),
  ('vista log_entries_vigentes con trazabilidad', exists (
     select 1 from information_schema.columns
     where table_schema = 'public' and table_name = 'log_entries_vigentes'
       and column_name = 'ultima_revision_autor_id')),
  ('bitácora insert-only (sin policies UPDATE/DELETE)', not exists (
     select 1 from pg_policies
     where schemaname = 'public' and tablename = 'log_entries' and cmd in ('UPDATE', 'DELETE'))),
  ('carga offline (log_entries.client_id)', exists (
     select 1 from information_schema.columns
     where table_schema = 'public' and table_name = 'log_entries' and column_name = 'client_id')),
  ('resúmenes asíncronos (summaries.estado)', exists (
     select 1 from information_schema.columns
     where table_schema = 'public' and table_name = 'summaries' and column_name = 'estado')),
  ('asistente estructurado (contexto_entry_ids)', exists (
     select 1 from information_schema.columns
     where table_schema = 'public' and table_name = 'assistant_messages'
       and column_name = 'contexto_entry_ids')),
  ('turnos con RLS (care_shifts)', coalesce((
     select relrowsecurity from pg_class where oid = to_regclass('public.care_shifts')), false)),
  ('helper puede_estar_de_turno', to_regprocedure('public.puede_estar_de_turno(uuid,uuid)') is not null)
) as verificacion(item, ok);
