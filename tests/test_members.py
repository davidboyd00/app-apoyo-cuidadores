"""Tests de la red de cuidado.

Cubren:
    - RPC invite_member_by_email: admin invita por email a cuenta existente.
    - RPC falla si el email no tiene cuenta.
    - RPC falla si quien invita no es admin.
    - RPC list_care_members: devuelve usuarios + perfil.
    - Trigger: no se puede eliminar al único admin.
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.rls


def _crear_paciente(sess, nombre: str) -> str:
    return sess.sql("insert into patients (nombre) values (%s) returning id", (nombre,))[0][0]


class TestInvite:
    def test_admin_invita_por_email(self, make_user):
        admin = make_user("admin@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        make_user("care@test.cl")  # crea la cuenta destino

        res = admin.sql(
            "select invite_member_by_email(%s, 'care@test.cl', 'caregiver')",
            (pid,),
        )
        assert res[0][0] is not None

        total = admin.sql(
            "select count(*) from care_members where patient_id = %s", (pid,)
        )[0][0]
        assert total == 2  # admin + nuevo caregiver

    def test_invite_falla_si_email_no_existe(self, make_user):
        admin = make_user("admin@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        with pytest.raises(psycopg.Error):
            admin.sql(
                "select invite_member_by_email(%s, 'nadie@test.cl', 'caregiver')",
                (pid,),
            )

    def test_invite_falla_si_no_soy_admin(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        otro = make_user("otro@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        # care (no admin) intenta invitar
        _ = otro  # cuenta destino solo para que exista
        with pytest.raises(psycopg.Error):
            care.sql(
                "select invite_member_by_email(%s, 'otro@test.cl', 'caregiver')",
                (pid,),
            )

    def test_reinvitar_actualiza_rol(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "viewer")

        admin.sql(
            "select invite_member_by_email(%s, 'care@test.cl', 'caregiver')",
            (pid,),
        )
        rol = admin.sql(
            "select role::text from care_members where patient_id = %s and user_id = %s",
            (pid, str(care.user_id)),
        )[0][0]
        assert rol == "caregiver"


class TestListMembers:
    def test_lista_devuelve_miembros_con_perfil_y_email(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        care.sql(
            "insert into profiles (id, nombre, telefono) values (auth.uid(), 'Carla', '+56911')"
        )

        rows = admin.sql("select * from list_care_members(%s)", (pid,))
        emails = {r[1] for r in rows}
        assert emails == {"admin@test.cl", "care@test.cl"}
        # Carla aparece con nombre y teléfono
        carla = next(r for r in rows if r[1] == "care@test.cl")
        assert carla[3] == "Carla"
        assert carla[4] == "+56911"

    def test_lista_falla_si_no_soy_miembro(self, make_user):
        admin = make_user("admin@test.cl")
        extraño = make_user("x@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")

        # list_care_members tiene is_member(...) interno → extraño ve 0 filas
        # (no error, pero resultado vacío porque no es miembro).
        rows = extraño.sql("select * from list_care_members(%s)", (pid,))
        assert rows == []


class TestUltimoAdmin:
    def test_no_puedo_eliminar_al_unico_admin(self, make_user):
        admin = make_user("admin@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")

        with pytest.raises(psycopg.errors.RaiseException):
            admin.sql(
                "delete from care_members where patient_id = %s and user_id = auth.uid()",
                (pid,),
            )

    def test_puedo_salir_si_hay_otro_admin(self, make_user, add_member):
        admin1 = make_user("admin1@test.cl")
        admin2 = make_user("admin2@test.cl")
        pid = _crear_paciente(admin1, "Doña Elba")
        add_member(pid, admin2.user_id, "admin")

        admin1.sql(
            "delete from care_members where patient_id = %s and user_id = auth.uid()",
            (pid,),
        )
        total = admin2.sql(
            "select count(*) from care_members where patient_id = %s", (pid,)
        )[0][0]
        assert total == 1
