"""Tests de los derechos ARCO (Ley 21.719) implementados en la base.

Cubren:
    - `delete_my_account()` reasigna author_id al placeholder y no deja
      referencias colgantes.
    - Borrar un paciente (DELETE cascade) no deja filas huérfanas en ninguna
      tabla hija.
    - El usuario placeholder es inmutable / no puede autoborrarse.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.rls

PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


def _crear_paciente(sess, nombre: str) -> str:
    return sess.sql("insert into patients (nombre) values (%s) returning id", (nombre,))[0][0]


class TestDeleteMyAccount:
    def test_anonimiza_bitacora_y_borra_cuenta(self, make_user, superuser):
        a = make_user("a@test.cl")
        pid = _crear_paciente(a, "Doña Elba")
        a.sql(
            "insert into log_entries (patient_id, kind, content, occurred_at) "
            "values (%s, 'nota', 'x', now())",
            (pid,),
        )
        uid = a.user_id

        # Ejercer supresión.
        a.sql("select delete_my_account()")

        # La entrada de bitácora sigue existiendo pero su autoría cambió al placeholder.
        with superuser.cursor() as cur:
            cur.execute("select author_id from log_entries where patient_id = %s", (str(pid),))
            row = cur.fetchone()
            assert row is None or str(row[0]) == PLACEHOLDER, (
                "el paciente era del user; debió eliminarse en cascada o quedar anonimizada"
            )

            # La cuenta ya no existe.
            cur.execute("select 1 from auth.users where id = %s", (str(uid),))
            assert cur.fetchone() is None

    def test_pacientes_con_otros_miembros_no_se_borran(self, make_user, add_member, superuser):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        # care se autoelimina; el paciente sigue existiendo (admin sigue).
        care.sql("select delete_my_account()")
        with superuser.cursor() as cur:
            cur.execute("select count(*) from patients where id = %s", (str(pid),))
            assert cur.fetchone()[0] == 1

    def test_placeholder_no_puede_autoborrarse(self, base_dsn, clean_data):
        # Actuamos como el usuario placeholder.
        import psycopg

        from tests.conftest import _connect

        conn = _connect(base_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("set role authenticated")
                cur.execute("select set_config('request.jwt.claim.sub', %s, false)", (PLACEHOLDER,))
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("select delete_my_account()")
        finally:
            conn.close()


class TestDeletePatientCascade:
    def test_borrar_paciente_no_deja_filas_huerfanas(self, make_user, add_member, superuser):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        # Poblar todas las tablas hijas.
        care.sql(
            "insert into log_entries (patient_id, kind, content, occurred_at) "
            "values (%s, 'nota', 'x', now())",
            (pid,),
        )
        admin.sql("insert into medications (patient_id, nombre) values (%s, 'paracetamol')", (pid,))
        admin.sql(
            "insert into summaries (patient_id, desde, hasta, contenido) "
            "values (%s, now() - interval '1 day', now(), 'resumen')",
            (pid,),
        )
        admin.sql(
            "insert into assistant_messages (patient_id, author_id, pregunta, respuesta) "
            "values (%s, auth.uid(), 'p', 'r')",
            (pid,),
        )

        # Admin borra el paciente.
        admin.sql("delete from patients where id = %s", (pid,))

        # Verificación cruda desde superuser: cero huérfanos.
        with superuser.cursor() as cur:
            for tabla in (
                "log_entries",
                "medications",
                "summaries",
                "assistant_messages",
                "care_members",
            ):
                cur.execute(f"select count(*) from {tabla} where patient_id = %s", (str(pid),))
                assert cur.fetchone()[0] == 0, f"quedaron huérfanas en {tabla}"
