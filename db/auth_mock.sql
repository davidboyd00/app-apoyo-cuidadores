-- ============================================================================
-- Mock del schema `auth` de Supabase para correr db/schema.sql en Postgres
-- local (verificación y tests de RLS). En Supabase real este schema ya existe
-- y este archivo NO se corre.
-- ============================================================================

create schema if not exists auth;

create table if not exists auth.users (
  id     uuid primary key default gen_random_uuid(),
  email  text unique
);

-- auth.uid() igual que en Supabase: lee el `sub` del JWT. PostgREST 12 lo
-- entrega en `request.jwt.claims` (JSON); los tests de RLS "actúan como" un
-- usuario seteando `request.jwt.claim.sub` directamente. Se aceptan ambas.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid;
$$;

-- Rol `authenticated` que emula el rol al que Supabase entrega los tokens
-- de usuario. Las políticas RLS no dependen del nombre del rol, pero lo
-- creamos para paridad con el entorno real.
do $$ begin
  create role authenticated;
exception when duplicate_object then null; end $$;

grant usage on schema public to authenticated;
grant usage on schema auth to authenticated;
grant select on auth.users to authenticated;
