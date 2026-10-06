# Setup del proyecto Supabase

Guía paso a paso para dejar la BD del backend corriendo en Supabase real.
Solo hay que hacerlo una vez por ambiente.

## 1. Crear el proyecto

1. Entrar a https://supabase.com/dashboard → **New project**.
2. Nombre: `caregivers-backend-<ambiente>` (ej. `caregivers-backend-prod`).
3. **Región: `sa-east-1` (São Paulo)** — es la más cercana a Chile en las
   opciones actuales de Supabase; latencia razonable para la app.
4. Password del rol `postgres`: generar aleatoria y **guardar** en el
   password manager del equipo (no en el repo, no en Slack).
5. Plan: **Free tier**.

Espera ~2 min a que provisione. En el dashboard, sección **Project Settings
→ API**, anota:
- `URL` (`SUPABASE_URL`)
- `anon public` key (`SUPABASE_ANON_KEY`)
- `service_role` key (`SUPABASE_SERVICE_ROLE_KEY`) — **jamás en el repo
  ni en el frontend; solo en secrets del scheduler de scripts**.
- `JWT Secret` (**Settings → API → JWT Settings → JWT Secret**;
  `SUPABASE_JWT_SECRET`).

## 2. Aplicar el schema

Desde **SQL Editor**:

1. Ejecutar el contenido completo de `db/schema.sql` en un solo run.
2. Debe terminar sin errores (los `NOTICE` de "policy does not exist,
   skipping" son esperados en instalación limpia).
3. NUNCA correr `db/auth_mock.sql` acá — es solo para tests locales.

### Verificar que las policies quedaron activas

```sql
select schemaname, tablename, count(*) as policies
from pg_policies
where schemaname = 'public'
group by 1, 2
order by 2;
```

**Salida esperada** (11 tablas, 25 policies):

| tabla                | policies |
|----------------------|----------|
| assistant_messages   | 2        |
| audit_log            | 1        |
| care_members         | 2        |
| centers              | 1        |
| consents             | 2        |
| log_entries          | 4        |
| medication_doses     | 2        |
| medications          | 2        |
| patients             | 4        |
| profiles             | 3        |
| summaries            | 2        |

Si algún número no coincide, revisar el output de `db/schema.sql` — algo
no corrió.

### Verificar que RLS está enabled

```sql
select relname, relrowsecurity
from pg_class
where relnamespace = 'public'::regnamespace
  and relkind = 'r'
order by relname;
```

**Todas las tablas deben tener `relrowsecurity = t`**. Excepto `centers`
que también lo tiene enabled pero con policy pública de lectura.

### Verificar helpers SECURITY DEFINER

```sql
select proname, prosecdef
from pg_proc
where pronamespace = 'public'::regnamespace
  and proname in (
    'is_member', 'has_role', 'shares_care_group',
    'delete_my_account', 'audit_write',
    'invite_member_by_email', 'list_care_members'
  );
```

Los 7 deben aparecer con `prosecdef = t`.

## 3. Placeholder de usuario eliminado

**No incluido en `db/schema.sql`** — `auth.users` en producción tiene
columnas obligatorias que el schema portable no puede rellenar. Correr
una sola vez desde el SQL Editor:

```sql
-- Ley 21.719 · usuario placeholder al que se reasignan author_id tras
-- ejercer derecho de supresión. UUID fijo, referenciado por la función
-- delete_my_account() del schema.
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
  raw_user_meta_data
)
values (
  '00000000-0000-0000-0000-000000000000',
  '00000000-0000-0000-0000-000000000000',
  'authenticated',
  'authenticated',
  'eliminado@caregivers.local',
  '',                                        -- sin password, no puede iniciar sesión
  now(),
  now(),
  now(),
  '{"provider":"placeholder","providers":[]}'::jsonb,
  '{}'::jsonb
)
on conflict (id) do nothing;
```

Verificación:

```sql
select id, email from auth.users
where id = '00000000-0000-0000-0000-000000000000';
```

Debe retornar 1 fila. Si retorna 0, `delete_my_account()` fallará con
`ForeignKeyViolation` cuando el primer usuario ejerza supresión.

## 4. Aplicar el `.env` en el backend

En `caregivers-backend/.env` (local) o en Render (producción):

```bash
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_JWT_SECRET=<jwt secret del dashboard>
# SUPABASE_SERVICE_ROLE_KEY solo en secrets de GitHub Actions (retention.py)
POLICY_VERSION=2026-09-11
```

Reiniciar el server. `GET /health` debe responder `200`.

## 5. Comprobación end-to-end

Desde el Dashboard **Authentication → Users → Add user** crear un usuario
de prueba (email + password) y usar su JWT (desde el cliente móvil o
`supabase.auth.signInWithPassword` en un script) para pegarle a
`GET /patients`. Debe retornar `[]` (RLS oculta lo que no le pertenece).

## 6. Migraciones futuras

No re-correr `db/schema.sql` en un proyecto ya vivo. Usar
`db/migrations/YYYYMMDD_slug.sql` con el delta (ver `db/migrations/README.md`).

## Troubleshooting

| Error                                                    | Causa                                       | Solución                                                   |
|----------------------------------------------------------|---------------------------------------------|------------------------------------------------------------|
| `permission denied for schema public`                    | Ejecutas como rol no-superuser              | Usar SQL Editor del dashboard (corre como `postgres`).     |
| `function auth.uid() does not exist`                     | El schema `auth` no está / rol wrong        | Comprobar que el proyecto Supabase esté sano.              |
| `role "authenticated" already exists`                    | Es esperado                                 | Los `do $$` con exception handler lo cachean; ignorar.     |
| `ForeignKeyViolation` al llamar `delete_my_account()`    | Falta el placeholder                        | Correr el INSERT del §3.                                   |
