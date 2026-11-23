-- ============================================================================
-- Mock del schema `auth` de Supabase para correr db/schema.sql en Postgres
-- local (verificación y tests de RLS). En Supabase real este schema ya existe
-- y este archivo NO se corre.
-- ============================================================================

create schema if not exists auth;

-- Además de (id, email), las columnas de Supabase Auth que rellena
-- db/seed_placeholder.sql, con los mismos nombres y tipos que en producción
-- (allá también son opcionales). Así el mismo INSERT corre en local y en Supabase.
create table if not exists auth.users (
  id                     uuid primary key default gen_random_uuid(),
  email                  text unique,
  instance_id            uuid,
  aud                    varchar(255),
  role                   varchar(255),
  encrypted_password     varchar(255),
  email_confirmed_at     timestamptz,
  confirmation_token     varchar(255),
  recovery_token         varchar(255),
  email_change_token_new varchar(255),
  email_change           varchar(255),
  raw_app_meta_data      jsonb,
  raw_user_meta_data     jsonb,
  created_at             timestamptz,
  updated_at             timestamptz
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
