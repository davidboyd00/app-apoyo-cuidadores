-- Roles que Supabase ya trae y que PostgREST necesita (solo stack local de
-- integración; en Supabase este archivo NO se corre).
do $$ begin create role anon nologin; exception when duplicate_object then null; end $$;
do $$ begin create role authenticator noinherit login; exception when duplicate_object then null; end $$;
grant anon to authenticator;
grant authenticated to authenticator;
grant usage on schema public to anon;
grant usage on schema auth to anon;
