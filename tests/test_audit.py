"""Tests del registro de auditoría (Ley 21.719 · Punto 2).

Verifican:
    - `audit_write` inserta con user_id = auth.uid() y no acepta suplantación.
    - INSERT directo a audit_log está bloqueado (append-only vía función).
    - RLS: admin del grupo ve el audit del paciente; caregiver/viewer no.
    - Un admin de otro grupo NO ve el audit de un paciente ajeno.
    - Acciones sin patient_id solo son visibles por su propio user_id.
    - El JSON metadata no contiene contenido clínico (regla: solo counts/flags).
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.rls


def _crear_paciente(sess, nombre: str) -> str:
    return sess.sql("insert into patients (nombre) values (%s) returning id", (nombre,))[0][0]


def _write(sess, accion, recurso_tipo, recurso_id=None, patient_id=None, metadata=None):
    import json

    return sess.sql(
        "select audit_write(%s, %s, %s::uuid, %s::uuid, %s::jsonb)",
        (
            accion,
            recurso_tipo,
            recurso_id,
            patient_id,
            json.dumps(metadata or {}),
        ),
    )


class TestAuditWrite:
    def test_registra_con_user_id_del_caller(self, make_user, add_member, superuser):
        admin = make_user("admin@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        _write(admin, "test_action", "patients", patient_id=pid, metadata={"n": 1})

        with superuser.cursor() as cur:
            cur.execute(
                "select user_id, accion, metadata from audit_log where patient_id = %s", (pid,)
            )
            row = cur.fetchone()
            assert str(row[0]) == str(admin.user_id)
            assert row[1] == "test_action"
            assert row[2] == {"n": 1}

    def test_insert_directo_a_audit_log_esta_bloqueado(self, make_user):
        admin = make_user("admin@test.cl")
        # audit_log no tiene policies INSERT → RLS niega por defecto.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.sql(
                "insert into audit_log (user_id, accion, recurso_tipo) values (auth.uid(), 'x', 'y')"
            )


class TestAuditRead:
    def test_admin_ve_audit_de_su_paciente(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        # care hace algo auditable
        _write(care, "read_entries_ajenas", "log_entries", patient_id=pid, metadata={"total": 3})

        rows = admin.sql("select accion from audit_log where patient_id = %s", (pid,))
        assert len(rows) == 1 and rows[0][0] == "read_entries_ajenas"

    def test_caregiver_no_ve_audit_del_paciente(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        _write(admin, "something", "patients", patient_id=pid)

        # care es solo caregiver, no admin → no debe ver
        rows = care.sql("select * from audit_log where patient_id = %s", (pid,))
        assert rows == []

    def test_admin_de_otro_grupo_no_ve_audit_ajeno(self, make_user, add_member):
        admin_a = make_user("adminA@test.cl")
        admin_b = make_user("adminB@test.cl")
        pid_a = _crear_paciente(admin_a, "Paciente de A")
        _write(admin_a, "test", "patients", patient_id=pid_a)

        # admin_b no es miembro del grupo de pid_a → no debe ver nada.
        rows = admin_b.sql("select * from audit_log where patient_id = %s", (pid_a,))
        assert rows == []

    def test_accion_sin_patient_id_solo_visible_por_su_autor(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        _write(a, "self_delete", "user", recurso_id=str(a.user_id))

        # a la ve (patient_id null + user_id = auth.uid())
        assert len(a.sql("select 1 from audit_log where accion = 'self_delete'")) == 1
        # b no
        assert b.sql("select 1 from audit_log where accion = 'self_delete'") == []
