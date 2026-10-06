"""Tests de la tabla profiles.

Cubren:
    - Ver mi propio perfil.
    - Ver perfiles de miembros con los que comparto grupo de cuidado.
    - NO ver perfiles de terceros sin relación.
    - Insertar/actualizar solo mi perfil.
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.rls


def _crear_paciente(sess, nombre: str) -> str:
    return sess.sql("insert into patients (nombre) values (%s) returning id", (nombre,))[0][0]


def _upsert_profile(sess, nombre: str, telefono: str = "+56912345678"):
    sess.sql(
        "insert into profiles (id, nombre, telefono) values (auth.uid(), %s, %s) "
        "on conflict (id) do update set nombre = excluded.nombre, telefono = excluded.telefono",
        (nombre, telefono),
    )


class TestVerPropioPerfil:
    def test_veo_mi_perfil(self, make_user):
        a = make_user("a@test.cl")
        _upsert_profile(a, "Ana")
        assert a.sql("select nombre from profiles where id = auth.uid()")[0][0] == "Ana"


class TestVerPerfilesDeRed:
    def test_veo_perfil_de_miembro_del_grupo(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        _upsert_profile(care, "Carla")

        row = admin.sql(
            "select nombre from profiles where id = %s", (str(care.user_id),)
        )
        assert row and row[0][0] == "Carla"

    def test_no_veo_perfil_de_persona_sin_grupo_compartido(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        _upsert_profile(b, "Bruno")

        row = a.sql("select count(*) from profiles where id = %s", (str(b.user_id),))
        assert row[0][0] == 0


class TestEscrituraSoloPropia:
    def test_no_puedo_insertar_perfil_de_otro(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            a.sql(
                "insert into profiles (id, nombre) values (%s, 'hack')",
                (str(b.user_id),),
            )

    def test_no_puedo_actualizar_perfil_de_otro(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        _upsert_profile(care, "Carla")

        # admin lee a Carla (sí) pero NO puede modificarla.
        admin.sql(
            "update profiles set nombre = 'HACK' where id = %s", (str(care.user_id),)
        )
        row = care.sql("select nombre from profiles where id = auth.uid()")
        assert row[0][0] == "Carla"
