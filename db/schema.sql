-- ============================================================================
-- Caregivers Backend · esquema de base + políticas RLS
-- Fuente de verdad del modelo de datos (sin migraciones automáticas por ahora).
-- Correr en el SQL Editor de Supabase.
-- ============================================================================

-- Extensiones ----------------------------------------------------------------
create extension if not exists "pgcrypto";  -- gen_random_uuid()

-- Enumerados -----------------------------------------------------------------
do $$ begin
  create type care_role as enum ('admin', 'caregiver', 'viewer');
exception when duplicate_object then null; end $$;

do $$ begin
  create type entry_kind as enum ('nota', 'medicamento', 'sintoma', 'animo', 'otro');
exception when duplicate_object then null; end $$;

-- Migración in-place: valores agregados post-creación del enum.
alter type entry_kind add value if not exists 'alimentacion';
alter type entry_kind add value if not exists 'ejercicio';

do $$ begin
  create type entry_source as enum ('text', 'voice');
exception when duplicate_object then null; end $$;

do $$ begin
  create type dose_status as enum ('tomada', 'omitida', 'postpuesta');
exception when duplicate_object then null; end $$;

-- ============================================================================
-- Tablas
-- ============================================================================

create table if not exists patients (
  id                uuid primary key default gen_random_uuid(),
  nombre            text not null,
  fecha_nacimiento  timestamptz,
  notas             text,
  created_by        uuid not null default auth.uid() references auth.users(id) on delete set null,
  created_at        timestamptz not null default now()
);

-- Membresía de cuidadores por paciente (grupo de cuidado).
create table if not exists care_members (
  patient_id  uuid not null references patients(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  role        care_role not null default 'caregiver',
  created_at  timestamptz not null default now(),
  primary key (patient_id, user_id)
);

create table if not exists log_entries (
  id           uuid primary key default gen_random_uuid(),
  patient_id   uuid not null references patients(id) on delete cascade,
  author_id    uuid not null default auth.uid() references auth.users(id) on delete restrict,
  kind         entry_kind not null,
  content      text not null,
  occurred_at  timestamptz not null,
  source       entry_source not null default 'text',
  created_at   timestamptz not null default now()
);
create index if not exists log_entries_patient_occurred_idx
  on log_entries (patient_id, occurred_at desc);

create table if not exists medications (
  id                uuid primary key default gen_random_uuid(),
  patient_id        uuid not null references patients(id) on delete cascade,
  nombre            text not null,
  dosis             text,
  frecuencia_horas  int check (frecuencia_horas is null or frecuencia_horas > 0),
  horarios          time[] not null default '{}',
  notas             text,
  activo            boolean not null default true,
  created_at        timestamptz not null default now()
);

-- Migración in-place: la columna `horarios` se agrega si no existe (para
-- bases que ya corrieron una versión anterior del schema).
alter table medications
  add column if not exists horarios time[] not null default '{}';

-- Registro de dosis (adherencia). Append-only por diseño: sirve como
-- evidencia clínica y alimenta el KPI de relevo familiar vía author_id.
-- `scheduled_for` es la hora en que correspondía; `taken_at` cuándo se tomó
-- efectivamente (null si el status es 'omitida').
create table if not exists medication_doses (
  id             uuid primary key default gen_random_uuid(),
  patient_id     uuid not null references patients(id) on delete cascade,
  medication_id  uuid not null references medications(id) on delete cascade,
  author_id      uuid not null default auth.uid() references auth.users(id) on delete restrict,
  scheduled_for  timestamptz not null,
  taken_at       timestamptz,
  status         dose_status not null,
  notas          text,
  created_at     timestamptz not null default now(),
  -- Coherencia: si se tomó, hay taken_at; si se omitió, no lo hay.
  check (
    (status = 'tomada'      and taken_at is not null) or
    (status = 'omitida'     and taken_at is null)     or
    (status = 'postpuesta')
  )
);
create index if not exists medication_doses_patient_sched_idx
  on medication_doses (patient_id, scheduled_for desc);
create index if not exists medication_doses_med_sched_idx
  on medication_doses (medication_id, scheduled_for);

create table if not exists summaries (
  id          uuid primary key default gen_random_uuid(),
  patient_id  uuid not null references patients(id) on delete cascade,
  desde       timestamptz not null,
  hasta       timestamptz not null,
  contenido   text not null,
  created_at  timestamptz not null default now()
);

create table if not exists assistant_messages (
  id                uuid primary key default gen_random_uuid(),
  patient_id        uuid not null references patients(id) on delete cascade,
  author_id         uuid not null default auth.uid() references auth.users(id) on delete restrict,
  conversation_id   uuid not null default gen_random_uuid(),
  pregunta          text not null,
  respuesta         text not null,
  cited_entry_ids   uuid[] not null default '{}',
  created_at        timestamptz not null default now()
);

-- Migración in-place para bases con la versión previa del schema.
alter table assistant_messages
  add column if not exists conversation_id uuid not null default gen_random_uuid();
create index if not exists assistant_messages_conv_idx
  on assistant_messages (patient_id, conversation_id, created_at);

-- Tablas públicas (pobladas por scrapers con service role, lectura anónima).
create table if not exists centers (
  id         uuid primary key default gen_random_uuid(),
  nombre     text not null,
  tipo       text not null,
  region     text not null,
  comuna     text,
  direccion  text,
  telefono   text
);

-- Perfil del cuidador. Separado de auth.users (que es gestionado por Supabase
-- Auth y no se debe tocar). Un usuario tiene un perfil opcional con sus datos
-- de contacto para que el resto de la red de cuidado lo pueda identificar.
create table if not exists profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  nombre      text,
  telefono    text,
  updated_at  timestamptz not null default now()
);

-- ============================================================================
-- Helpers (SECURITY DEFINER para evitar recursión en políticas RLS)
-- ============================================================================

create or replace function is_member(pid uuid)
returns boolean
language sql
security definer
set search_path = public
as $$
  select exists (
    select 1 from care_members
    where patient_id = pid and user_id = auth.uid()
  );
$$;

create or replace function has_role(pid uuid, r care_role)
returns boolean
language sql
security definer
set search_path = public
as $$
  select exists (
    select 1 from care_members
    where patient_id = pid and user_id = auth.uid() and role = r
  );
$$;

-- Devuelve true si auth.uid() comparte al menos un grupo de cuidado con el
-- usuario `target`. Se usa en profiles_select para que los miembros de la red
-- se vean entre sí sin exponer perfiles de terceros. SECURITY DEFINER evita
-- recursión al consultar care_members desde una policy de profiles.
create or replace function shares_care_group(target uuid)
returns boolean
language sql
security definer
set search_path = public
as $$
  select exists (
    select 1
    from care_members me
    inner join care_members other
      on me.patient_id = other.patient_id
    where me.user_id = auth.uid()
      and other.user_id = target
  );
$$;

-- ============================================================================
-- Row-Level Security
-- ============================================================================

alter table patients            enable row level security;
alter table care_members        enable row level security;
alter table log_entries         enable row level security;
alter table medications         enable row level security;
alter table medication_doses    enable row level security;
alter table summaries           enable row level security;
alter table assistant_messages  enable row level security;
alter table centers             enable row level security;
alter table profiles            enable row level security;

-- patients: ver = ser miembro O ser el creador (created_by = uid).
-- El OR es imprescindible: `INSERT ... RETURNING` aplica esta USING sobre la
-- fila recién creada, y `is_member` (STABLE) no ve aún la fila que el
-- AFTER TRIGGER acaba de escribir en care_members dentro del mismo statement.
-- Semánticamente coincide: quien crea el paciente queda como admin del grupo.
drop policy if exists patients_select on patients;
create policy patients_select on patients
  for select using (is_member(id) or created_by = auth.uid());

drop policy if exists patients_insert on patients;
create policy patients_insert on patients
  for insert with check (auth.uid() is not null);

drop policy if exists patients_update on patients;
create policy patients_update on patients
  for update using (has_role(id, 'admin'));

drop policy if exists patients_delete on patients;
create policy patients_delete on patients
  for delete using (has_role(id, 'admin'));

-- care_members: ver miembros de mis pacientes; admin gestiona.
drop policy if exists care_members_select on care_members;
create policy care_members_select on care_members
  for select using (is_member(patient_id));

drop policy if exists care_members_write on care_members;
create policy care_members_write on care_members
  for all using (has_role(patient_id, 'admin'))
  with check (has_role(patient_id, 'admin'));

-- log_entries: leer si soy miembro; escribir si soy admin o caregiver.
drop policy if exists entries_select on log_entries;
create policy entries_select on log_entries
  for select using (is_member(patient_id));

drop policy if exists entries_insert on log_entries;
create policy entries_insert on log_entries
  for insert with check (
    (has_role(patient_id, 'admin') or has_role(patient_id, 'caregiver'))
    and author_id = auth.uid()
  );

-- Update/delete de entradas: SOLO el autor original o un admin del grupo.
-- Regla acordada con el equipo (no cubierta por CLAUDE.md original):
-- balancea corrección de errores tipográficos con trazabilidad de autoría.
-- El WITH CHECK previene además "reasignar" la autoría de la entrada.
drop policy if exists entries_update on log_entries;
create policy entries_update on log_entries
  for update
  using (author_id = auth.uid() or has_role(patient_id, 'admin'))
  with check (author_id = auth.uid() or has_role(patient_id, 'admin'));

drop policy if exists entries_delete on log_entries;
create policy entries_delete on log_entries
  for delete
  using (author_id = auth.uid() or has_role(patient_id, 'admin'));

-- medications: leer si soy miembro; admin/caregiver gestionan.
drop policy if exists meds_select on medications;
create policy meds_select on medications
  for select using (is_member(patient_id));

drop policy if exists meds_write on medications;
create policy meds_write on medications
  for all using (has_role(patient_id, 'admin') or has_role(patient_id, 'caregiver'))
  with check (has_role(patient_id, 'admin') or has_role(patient_id, 'caregiver'));

-- medication_doses: leer si soy miembro; escribir admin/caregiver con
-- author_id = auth.uid() para preservar KPI de relevo familiar. Append-only
-- a propósito (sin policies UPDATE/DELETE): la adherencia es evidencia clínica.
drop policy if exists doses_select on medication_doses;
create policy doses_select on medication_doses
  for select using (is_member(patient_id));

drop policy if exists doses_insert on medication_doses;
create policy doses_insert on medication_doses
  for insert with check (
    (has_role(patient_id, 'admin') or has_role(patient_id, 'caregiver'))
    and author_id = auth.uid()
  );

-- summaries: leer si soy miembro; escribir admin/caregiver.
drop policy if exists summaries_select on summaries;
create policy summaries_select on summaries
  for select using (is_member(patient_id));

drop policy if exists summaries_insert on summaries;
create policy summaries_insert on summaries
  for insert with check (has_role(patient_id, 'admin') or has_role(patient_id, 'caregiver'));

-- assistant_messages: leer si soy miembro; escribir admin/caregiver.
drop policy if exists asst_select on assistant_messages;
create policy asst_select on assistant_messages
  for select using (is_member(patient_id));

drop policy if exists asst_insert on assistant_messages;
create policy asst_insert on assistant_messages
  for insert with check (
    (has_role(patient_id, 'admin') or has_role(patient_id, 'caregiver'))
    and author_id = auth.uid()
  );

-- centers: lectura pública, escritura solo service role (RLS bloquea al resto).
drop policy if exists centers_read on centers;
create policy centers_read on centers for select using (true);

-- profiles: ver mi propio perfil + perfiles de usuarios con los que comparto
-- al menos un grupo de cuidado (regla acordada: la red se identifica entre sí
-- pero no expone perfiles de terceros). Escritura solo propia.
drop policy if exists profiles_select on profiles;
create policy profiles_select on profiles
  for select using (id = auth.uid() or shares_care_group(id));

drop policy if exists profiles_insert on profiles;
create policy profiles_insert on profiles
  for insert with check (id = auth.uid());

drop policy if exists profiles_update on profiles;
create policy profiles_update on profiles
  for update using (id = auth.uid()) with check (id = auth.uid());

-- ============================================================================
-- Trigger: quien crea el paciente queda como admin del grupo automáticamente.
-- ============================================================================

create or replace function _patient_owner_admin()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.created_by is not null then
    insert into care_members (patient_id, user_id, role)
    values (new.id, new.created_by, 'admin')
    on conflict do nothing;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_patient_owner_admin on patients;
create trigger trg_patient_owner_admin
  after insert on patients
  for each row execute function _patient_owner_admin();

-- ============================================================================
-- GRANTS al rol `authenticated`
-- En Supabase este rol se otorga automáticamente al presentar un JWT válido.
-- Los explicitamos acá para que el schema sea autocontenido: RLS es lo que
-- realmente decide qué filas se ven, pero el GRANT es el requisito previo.
-- ============================================================================

do $$ begin
  create role authenticated;
exception when duplicate_object then null; end $$;

grant usage on schema public to authenticated;
grant select, insert, update, delete on all tables in schema public to authenticated;
alter default privileges in schema public
  grant select, insert, update, delete on tables to authenticated;

-- Rol anónimo (lectura pública de `centers`).
do $$ begin
  create role anon;
exception when duplicate_object then null; end $$;
grant usage on schema public to anon;
grant select on centers to anon;

-- ============================================================================
-- Ley 21.719 · Punto 1: derechos ARCO (Acceso, Rectificación, Cancelación,
-- Oposición). Implementación operativa; ver docs/REGISTRO_TRATAMIENTO.md.
-- ============================================================================

-- Usuario placeholder "eliminado". Cuando un titular ejerce el derecho de
-- supresión de su cuenta, sus entradas de bitácora NO se borran (son
-- historial clínico del paciente, base de licitud: interés legítimo del
-- grupo de cuidado y salud del titular del dato = paciente). En vez, su
-- author_id se reasigna a este usuario para preservar el registro histórico
-- sin poder trazar de vuelta al ex-cuidador.
--
-- El INSERT de esta fila NO va acá porque `auth.users` en Supabase real
-- tiene columnas obligatorias que este schema no puede rellenar de forma
-- portable. Se crea con `db/seed_placeholder.sql` (local) o con el paso
-- documentado en `docs/SUPABASE.md` (producción).

-- RPC: DELETE /me. Corre como SECURITY DEFINER para poder tocar auth.users
-- y reasignar author_id (que el user no puede modificar directamente por RLS).
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
  update log_entries        set author_id = placeholder where author_id = me;
  update assistant_messages set author_id = placeholder where author_id = me;

  -- Sale del grupo de cuidado. Los pacientes de los que era único miembro
  -- quedan huérfanos → los eliminamos en cascada (nadie puede acceder ni
  -- ejercer derechos sobre ellos, y su historial no debe quedar en el vacío).
  delete from care_members where user_id = me;
  delete from patients where id not in (select distinct patient_id from care_members);

  -- Finalmente elimina la cuenta de auth (Supabase la maneja como cualquier
  -- fila de auth.users; en el mock local también).
  delete from auth.users where id = me;
end;
$$;

revoke all on function delete_my_account() from public;
grant execute on function delete_my_account() to authenticated;

-- ============================================================================
-- Ley 21.719 · Punto 2: registro de actividades y auditoría.
--
-- Cumplimos con "evidencia operativa": qué usuario tocó qué recurso y cuándo.
-- Regla dura: JAMÁS contenido clínico dentro del log (ids y metadatos secos).
-- La escritura pasa por una función SECURITY DEFINER que fija el user_id con
-- auth.uid() — no se puede falsificar desde el cliente.
-- ============================================================================

create table if not exists audit_log (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid references auth.users(id) on delete set null,
  accion        text not null,
  recurso_tipo  text not null,
  recurso_id    uuid,
  patient_id    uuid,  -- nullable: hay acciones sin paciente (ej. delete_me)
  metadata      jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now()
);
create index if not exists audit_log_patient_created_idx
  on audit_log (patient_id, created_at desc);
create index if not exists audit_log_user_created_idx
  on audit_log (user_id, created_at desc);

alter table audit_log enable row level security;

-- Lectura: admin del grupo puede ver la auditoría del paciente.
-- El propio usuario puede ver sus acciones sin patient_id (ej. delete_me).
drop policy if exists audit_read on audit_log;
create policy audit_read on audit_log
  for select using (
    (patient_id is not null and has_role(patient_id, 'admin'))
    or (patient_id is null and user_id = auth.uid())
  );

-- Escritura y modificación: BLOQUEADAS para el rol authenticated. Solo se
-- escribe vía la función audit_write() con SECURITY DEFINER.
-- (No creamos policies para insert/update/delete → RLS niega por defecto.)

create or replace function audit_write(
  p_accion       text,
  p_recurso_tipo text,
  p_recurso_id   uuid  default null,
  p_patient_id   uuid  default null,
  p_metadata     jsonb default '{}'::jsonb
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if auth.uid() is null then
    -- Sin sesión (ej. scripts con service role): no auditamos automáticamente;
    -- si el llamador quiere registrar, debe usar service role y otro camino.
    return;
  end if;
  insert into audit_log (user_id, accion, recurso_tipo, recurso_id, patient_id, metadata)
  values (auth.uid(), p_accion, p_recurso_tipo, p_recurso_id, p_patient_id, coalesce(p_metadata, '{}'::jsonb));
end;
$$;

revoke all on function audit_write(text, text, uuid, uuid, jsonb) from public;
grant execute on function audit_write(text, text, uuid, uuid, jsonb) to authenticated;

-- ============================================================================
-- Ley 21.719 · Punto 3: consentimiento versionado.
--
-- El titular acepta una versión concreta de la política. La API bloquea
-- (409) cualquier operación sensible si el user no aceptó la vigente.
-- Append-only: aceptar es un evento datado; nunca se modifica ni borra la
-- fila (evidencia del momento del consentimiento).
-- ============================================================================

create table if not exists consents (
  user_id      uuid not null references auth.users(id) on delete cascade,
  version      text not null,
  aceptado_en  timestamptz not null default now(),
  primary key (user_id, version)
);

alter table consents enable row level security;

drop policy if exists consents_select on consents;
create policy consents_select on consents
  for select using (user_id = auth.uid());

drop policy if exists consents_insert on consents;
create policy consents_insert on consents
  for insert with check (user_id = auth.uid());

-- No policies UPDATE/DELETE → RLS bloquea. Es append-only.

-- ============================================================================
-- Red de cuidado · RPCs e invariantes
--
-- La invitación requiere que el invitado ya tenga cuenta (MVP). Si no la
-- tiene, el admin tiene que pedírselo que se registre primero — el flujo
-- de alta de cuenta vive en Supabase Auth, no en esta API.
-- ============================================================================

-- Invita un usuario existente al grupo por email. SECURITY DEFINER porque
-- necesita leer auth.users (que no es visible al rol authenticated).
create or replace function invite_member_by_email(
  p_patient_id uuid,
  p_email      text,
  p_role       care_role default 'caregiver'
) returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  target_id uuid;
begin
  if not has_role(p_patient_id, 'admin') then
    raise exception 'solo un admin del grupo puede invitar miembros'
      using errcode = '42501';
  end if;

  select id into target_id
  from auth.users
  where lower(email) = lower(p_email)
  limit 1;

  if target_id is null then
    raise exception 'no existe cuenta con el correo %', p_email
      using errcode = 'P0002';
  end if;

  insert into care_members (patient_id, user_id, role)
  values (p_patient_id, target_id, p_role)
  on conflict (patient_id, user_id) do update
    set role = excluded.role;

  return target_id;
end;
$$;

revoke all on function invite_member_by_email(uuid, text, care_role) from public;
grant execute on function invite_member_by_email(uuid, text, care_role) to authenticated;

-- Lista los miembros del grupo con su perfil + email. SECURITY DEFINER para
-- poder leer auth.users; el `is_member` interno enforcea que solo miembros
-- del grupo lo puedan llamar.
create or replace function list_care_members(p_patient_id uuid)
returns table (
  user_id    uuid,
  email      text,
  role       care_role,
  nombre     text,
  telefono   text,
  joined_at  timestamptz
)
language sql
security definer
set search_path = public
as $$
  select
    cm.user_id,
    u.email::text,
    cm.role,
    p.nombre,
    p.telefono,
    cm.created_at as joined_at
  from care_members cm
  left join profiles  p on p.id = cm.user_id
  left join auth.users u on u.id = cm.user_id
  where cm.patient_id = p_patient_id
    and is_member(p_patient_id);
$$;

revoke all on function list_care_members(uuid) from public;
grant execute on function list_care_members(uuid) to authenticated;

-- Invariante: no se puede eliminar al último admin del grupo (quedaría sin
-- quién administre). Para transferir admin: primero crear uno nuevo, después
-- quitarse. Las operaciones de delete pasan por aquí (RLS ya exige admin).
create or replace function _no_remove_last_admin()
returns trigger
language plpgsql
as $$
begin
  if old.role = 'admin' then
    if (
      select count(*)
      from care_members
      where patient_id = old.patient_id and role = 'admin'
    ) <= 1 then
      raise exception 'no se puede eliminar al único admin del grupo'
        using errcode = 'P0001';
    end if;
  end if;
  return old;
end;
$$;

drop trigger if exists trg_no_remove_last_admin on care_members;
create trigger trg_no_remove_last_admin
  before delete on care_members
  for each row execute function _no_remove_last_admin();

-- ============================================================================
-- Ley 21.719 · Punto 4: minimización.
--
-- Revisión: todo campo de tablas con datos personales/clínicos tiene una
-- justificación funcional concreta. Los comentarios COMMENT ON son
-- consultables (`\d+` en psql o information_schema.columns) y sirven como
-- evidencia operativa del principio de minimización en un eventual control.
--
-- Regla: si algún campo pierde su justificación, se elimina en el mismo PR
-- que retire su uso. Nunca "por si acaso".
-- ============================================================================

comment on table  patients                  is 'Persona dependiente cuidada por un grupo. Categoría: datos personales + sensibles (salud).';
comment on column patients.nombre           is 'Identificación humana del paciente en la UI del grupo. Único identificador legible.';
comment on column patients.fecha_nacimiento is 'Opcional. Edad ajusta interpretación clínica en resúmenes IA. Nullable si el titular no la aporta.';
comment on column patients.notas            is 'Opcional. Contexto clínico general (alergias, comorbilidades). Editable por admin/caregiver.';
comment on column patients.created_by       is 'Trigger _patient_owner_admin lo usa para asignar rol admin al creador. Referenciado por policy patients_select.';
comment on column patients.created_at       is 'Ordering histórico y trazabilidad interna. No expuesto en UI de otros usuarios.';

comment on table  log_entries               is 'Bitácora clínica compartida. Append-only en el patrón; edición restringida al autor o admin. Categoría: dato sensible (salud).';
comment on column log_entries.author_id     is 'KPI de relevo familiar (>60%). Regla sagrada #5. Sobrevive a delete_my_account: se reasigna al usuario placeholder.';
comment on column log_entries.kind          is 'Tipo enumerado (nota, medicamento, síntoma, ánimo, otro). Permite filtros y estadísticas sin leer content.';
comment on column log_entries.content       is 'Texto libre. NUNCA se pone en audit_log ni en logs de aplicación.';
comment on column log_entries.occurred_at   is 'Momento del hecho registrado. Distinto de created_at porque el cuidador puede anotar horas después.';
comment on column log_entries.source        is 'text|voice. La transcripción se hace en el cliente; el backend nunca recibe audio (ARCHITECTURE.md §5).';

comment on table  medications               is 'Régimen medicamentoso del paciente. Categoría: dato sensible (salud).';
comment on column medications.horarios      is 'Horas del día en que corresponde administrar. Base para /medications/upcoming (cliente agenda recordatorios locales).';
comment on column medications.activo        is 'Soft-delete: preserva historial de adherencia (útil clínicamente).';

comment on table  medication_doses          is 'Registro de dosis administradas/omitidas. Categoría: dato sensible (salud + adherencia farmacológica). Append-only.';
comment on column medication_doses.author_id     is 'Quién marcó la dosis. Alimenta KPI de relevo familiar (>60%) igual que log_entries.author_id.';
comment on column medication_doses.scheduled_for is 'Hora en que correspondía tomar según `medications.horarios`. Puede diferir de taken_at.';
comment on column medication_doses.taken_at      is 'Hora real de administración. NULL si status=omitida. Distinto de created_at (puede registrarse tarde).';
comment on column medication_doses.status        is 'tomada|omitida|postpuesta. postpuesta = se movió para más tarde; deberá generar otro registro cuando ocurra.';
comment on column medication_doses.notas         is 'Contexto opcional (ej. "vomitó a los 10min"). NUNCA se envía al audit_log.';

comment on table  assistant_messages        is 'Historial IA. `cited_entry_ids` sostiene el KPI >90% verificable. Retención acotada (scripts/retention.py).';
comment on column assistant_messages.cited_entry_ids is 'IDs de las entradas de bitácora usadas como contexto. Regla sagrada #4.';
comment on column assistant_messages.respuesta is 'Texto generado por LLM. Contiene información derivada de bitácora → dato sensible. Nunca en logs.';

comment on table  audit_log                 is 'Registro de actividades sensibles. Ley 21.719: evidencia operativa. metadata JAMÁS contiene contenido clínico.';
comment on table  consents                  is 'Aceptación versionada de la política. Append-only (evidencia del momento del consentimiento).';

comment on table  profiles                  is 'Perfil del cuidador (nombre, teléfono) separado de auth.users. Categoría: dato personal. Expuesto solo a usuarios de la misma red de cuidado.';
comment on column profiles.nombre           is 'Nombre visible en la red de cuidado. Finalidad: identificar al autor de entradas y a los demás miembros.';
comment on column profiles.telefono         is 'Teléfono de contacto, opcional. Finalidad: coordinación entre miembros ante emergencias.';
