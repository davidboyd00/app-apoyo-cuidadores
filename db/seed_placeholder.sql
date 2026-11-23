-- ============================================================================
-- Seed del usuario placeholder "eliminado" (Ley 21.719 · derecho de supresión).
--
-- Cuando alguien elimina su cuenta, sus registros se conservan para el grupo
-- con author_id = este usuario (ver delete_my_account() en db/schema.sql).
-- Sin esta fila, eliminar una cuenta con registros falla por la llave foránea.
--
-- El MISMO INSERT corre en los dos lados:
--   - En LOCAL/CI: los tests lo corren después de db/auth_mock.sql (el mock
--     tiene estas columnas).
--   - En SUPABASE: va incluido en db/APLICAR_EN_SUPABASE.sql. Correrlo solo
--     también sirve (SQL Editor); es idempotente.
--
-- Columnas como las que Supabase Auth deja en un usuario real. Los tokens van
-- en '' y no en null: Supabase Auth falla al leer un usuario con null en
-- ellos, y eso rompería la lista de usuarios del panel.
-- Sin contraseña (encrypted_password = '') y con un correo que no recibe
-- mensajes: nadie puede iniciar sesión con esta cuenta.
-- ============================================================================

insert into auth.users (
  instance_id,
  id,
  aud,
  role,
  email,
  encrypted_password,
  email_confirmed_at,
  created_at,
  updated_at,
  raw_app_meta_data,
  raw_user_meta_data,
  confirmation_token,
  recovery_token,
  email_change_token_new,
  email_change
)
values (
  '00000000-0000-0000-0000-000000000000',
  '00000000-0000-0000-0000-000000000000',
  'authenticated',
  'authenticated',
  'eliminado@caregivers.local',
  '',
  now(),
  now(),
  now(),
  '{"provider":"placeholder","providers":[]}'::jsonb,
  '{}'::jsonb,
  '',
  '',
  '',
  ''
)
on conflict (id) do nothing;
