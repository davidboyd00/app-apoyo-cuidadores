"""Tests del historial agrupado del asistente."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.rls

CONV_A = "11111111-1111-1111-1111-111111111111"
CONV_B = "22222222-2222-2222-2222-222222222222"


def _crear_paciente(sess, nombre: str) -> str:
    return sess.sql("insert into patients (nombre) values (%s) returning id", (nombre,))[0][0]


class TestConversationsAgrupado:
    def test_dos_conversaciones_del_mismo_paciente_se_agrupan(
        self, make_user, add_member, superuser
    ):
        admin = make_user("admin@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")

        # 2 mensajes en conv A y 1 en conv B, con timestamps distintos.
        with superuser.cursor() as cur:
            cur.execute(
                "insert into assistant_messages "
                "(patient_id, author_id, conversation_id, pregunta, respuesta, created_at) "
                "values (%s, %s, %s, 'q1', 'r1', now() - interval '2 hours'),"
                "       (%s, %s, %s, 'q2', 'r2', now() - interval '1 hour'),"
                "       (%s, %s, %s, 'qb', 'rb', now())",
                (
                    str(pid),
                    str(admin.user_id),
                    CONV_A,
                    str(pid),
                    str(admin.user_id),
                    CONV_A,
                    str(pid),
                    str(admin.user_id),
                    CONV_B,
                ),
            )

        rows = admin.sql(
            "select conversation_id, count(*) from assistant_messages "
            "where patient_id = %s group by conversation_id",
            (str(pid),),
        )
        d = {str(r[0]): r[1] for r in rows}
        assert d == {CONV_A: 2, CONV_B: 1}

    def test_conversacion_ajena_no_visible(self, make_user, superuser):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        pid_a = _crear_paciente(a, "Paciente de A")
        with superuser.cursor() as cur:
            cur.execute(
                "insert into assistant_messages "
                "(patient_id, author_id, conversation_id, pregunta, respuesta) "
                "values (%s, %s, %s, 'q', 'r')",
                (str(pid_a), str(a.user_id), CONV_A),
            )
        # b no es miembro del grupo de pid_a → RLS oculta.
        assert (
            b.sql(
                "select 1 from assistant_messages where conversation_id = %s",
                (CONV_A,),
            )
            == []
        )
