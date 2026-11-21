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
