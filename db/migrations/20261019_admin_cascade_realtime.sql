-- Descripción: trigger de último admin compatible con cascadas, delete_my_account reordenado y log_entries en Realtime.
--
-- Idempotente: se puede correr más de una vez. Corresponde a los commits
-- 1f5b216 (fix(db) trigger de último admin) y fc4f2b5 (feat(realtime)).
-- Ambas funciones son SECURITY DEFINER con search_path fijo.

-- 1) Trigger de último admin: permite el delete cuando viene de la cascada
--    de `delete from patients` (el paciente ya no existe). El trigger
--    trg_no_remove_last_admin no cambia: sigue apuntando a esta función.
create or replace function _no_remove_last_admin()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if not exists (select 1 from patients where id = old.patient_id) then
    return old;
  end if;
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

-- 2) DELETE /me: borra primero los pacientes donde el usuario es el único
--    miembro (su membresía se va por la cascada) y después sale del resto.
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

-- create or replace conserva los permisos; se reafirman igual que en schema.sql.
revoke all on function delete_my_account() from public;
grant execute on function delete_my_account() to authenticated;

-- 3) Realtime: log_entries en la publicación (solo si aún no está).
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (
       select 1 from pg_publication_tables
       where pubname = 'supabase_realtime'
         and schemaname = 'public'
         and tablename = 'log_entries'
     ) then
    alter publication supabase_realtime add table public.log_entries;
  end if;
end
$$;

-- 4) Verificación (solo lectura). Esperado:
--    a) dos filas, prosecdef = true y search_path=public en ambas.
select p.proname, p.prosecdef, p.proconfig
from pg_proc p
where p.pronamespace = 'public'::regnamespace
  and p.proname in ('delete_my_account', '_no_remove_last_admin')
order by p.proname;

--    b) una fila: el trigger activo (tgenabled = 'O') apuntando a la función.
select t.tgname, t.tgenabled, t.tgfoid::regproc as funcion
from pg_trigger t
where t.tgrelid = 'public.care_members'::regclass
  and t.tgname = 'trg_no_remove_last_admin';

--    c) una fila: log_entries está en la publicación de Realtime.
select pubname, schemaname, tablename
from pg_publication_tables
where pubname = 'supabase_realtime'
  and schemaname = 'public'
  and tablename = 'log_entries';
