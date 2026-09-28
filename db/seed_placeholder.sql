-- ============================================================================
-- Seed del usuario placeholder "eliminado" (Ley 21.719 · derecho de supresión).
--
-- Este archivo se ejecuta:
--   - En LOCAL/CI: automáticamente desde `db/auth_mock.sql` (tests).
--   - En SUPABASE producción: se corre UNA VEZ desde el SQL Editor tras
--     `db/schema.sql`. Ver `docs/SUPABASE.md` § "Placeholder de usuario
--     eliminado" — usa un INSERT con las columnas obligatorias reales de
--     auth.users que Supabase agrega (aud, role, encrypted_password, etc.).
--
-- Para el mock local, `auth.users` tiene solo (id, email), así que el INSERT
-- mínimo alcanza.
-- ============================================================================

insert into auth.users (id, email)
values ('00000000-0000-0000-0000-000000000000', 'eliminado@caregivers.local')
on conflict (id) do nothing;
