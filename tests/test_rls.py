"""Tests de Row-Level Security.

Cubren la regla sagrada de CLAUDE.md: un bug en un endpoint NO puede filtrar
datos entre pacientes, porque la última línea de defensa es la base.

Casos:
    - Aislamiento entre grupos (usuario A vs usuario B).
    - Rol viewer: lee pero no escribe.
    - Rol caregiver: escribe pero no borra pacientes.
    - Rol admin: puede update/delete del paciente.
    - `author_id` de log_entries siempre queda = auth.uid() (regla sagrada #5).
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.rls


def _crear_paciente(sess, nombre: str) -> str:
    return sess.sql("insert into patients (nombre) values (%s) returning id", (nombre,))[0][0]


# ---------------------------------------------------------------------------
# Aislamiento entre grupos (usuario A vs usuario B)
# ---------------------------------------------------------------------------


class TestAislamientoEntreUsuarios:
    def test_B_no_ve_paciente_de_A(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        _crear_paciente(a, "Doña Elba")

        assert a.sql("select count(*) from patients")[0][0] == 1
        assert b.sql("select count(*) from patients")[0][0] == 0

    def test_B_no_ve_bitacora_de_A(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        pid = _crear_paciente(a, "Don Juan")
        a.sql(
            "insert into log_entries (patient_id, kind, content, occurred_at) "
            "values (%s, 'nota', 'tomó desayuno', now())",
            (pid,),
        )
        assert a.sql("select count(*) from log_entries")[0][0] == 1
        assert b.sql("select count(*) from log_entries")[0][0] == 0

    def test_B_no_puede_insertar_bitacora_en_paciente_de_A(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        pid = _crear_paciente(a, "Don Juan")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            b.sql(
                "insert into log_entries (patient_id, author_id, kind, content, occurred_at) "
                "values (%s, auth.uid(), 'nota', 'hack', now())",
                (pid,),
            )

    def test_B_no_puede_actualizar_paciente_de_A(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        _crear_paciente(a, "Don Juan")

        # update sobre 0 filas: no error, pero tampoco efecto (RLS filtra en el USING).
        b.sql("update patients set nombre = 'HACKED'")
        # verificar desde A que su paciente sigue igual
        assert a.sql("select nombre from patients")[0][0] == "Don Juan"


# ---------------------------------------------------------------------------
# Roles dentro de un mismo grupo de cuidado
# ---------------------------------------------------------------------------


class TestRoles:
    def test_viewer_puede_leer_pero_no_escribir(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        viewer = make_user("viewer@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, viewer.user_id, "viewer")

        # lee
        assert viewer.sql("select count(*) from patients")[0][0] == 1

        # no puede insertar bitácora
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            viewer.sql(
                "insert into log_entries (patient_id, author_id, kind, content, occurred_at) "
                "values (%s, auth.uid(), 'nota', 'x', now())",
                (pid,),
            )

        # no puede insertar medicamento
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            viewer.sql(
                "insert into medications (patient_id, nombre) values (%s, 'paracetamol')",
                (pid,),
            )

    def test_caregiver_puede_escribir_bitacora(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        care.sql(
            "insert into log_entries (patient_id, author_id, kind, content, occurred_at) "
            "values (%s, auth.uid(), 'nota', 'sonrió hoy', now())",
            (pid,),
        )
        assert care.sql("select count(*) from log_entries")[0][0] == 1

    def test_caregiver_no_puede_borrar_paciente(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        care.sql("delete from patients")  # RLS bloquea → 0 afectadas, sin error
        assert admin.sql("select count(*) from patients")[0][0] == 1

    def test_admin_puede_borrar_paciente(self, make_user):
        admin = make_user("admin@test.cl")
        _crear_paciente(admin, "Doña Elba")
        admin.sql("delete from patients")
        assert admin.sql("select count(*) from patients")[0][0] == 0


# ---------------------------------------------------------------------------
# Edición / borrado de entradas: solo autor o admin del grupo
# ---------------------------------------------------------------------------


class TestEdicionEntradas:
    def _crear_entry(self, sess, patient_id: str, texto: str = "original") -> str:
        return sess.sql(
            "insert into log_entries (patient_id, kind, content, occurred_at) "
            "values (%s, 'nota', %s, now()) returning id",
            (patient_id, texto),
        )[0][0]

    def test_autor_puede_editar_su_entrada(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        eid = self._crear_entry(care, pid, "original")
        care.sql("update log_entries set content = 'corregido' where id = %s", (eid,))
        assert (
            care.sql("select content from log_entries where id = %s", (eid,))[0][0] == "corregido"
        )

    def test_admin_puede_editar_entrada_ajena(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid, "original")

        admin.sql("update log_entries set content = 'admin editó' where id = %s", (eid,))
        assert (
            admin.sql("select content from log_entries where id = %s", (eid,))[0][0]
            == "admin editó"
        )

    def test_caregiver_no_puede_editar_entrada_de_otro(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care_a = make_user("cA@test.cl")
        care_b = make_user("cB@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care_a.user_id, "caregiver")
        add_member(pid, care_b.user_id, "caregiver")
        eid = self._crear_entry(care_a, pid, "de A")

        # UPDATE con RLS que no matchea → 0 filas afectadas, sin error.
        care_b.sql("update log_entries set content = 'hack' where id = %s", (eid,))
        assert care_a.sql("select content from log_entries where id = %s", (eid,))[0][0] == "de A"

    def test_autor_puede_borrar_su_entrada(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid)

        care.sql("delete from log_entries where id = %s", (eid,))
        assert care.sql("select count(*) from log_entries")[0][0] == 0

    def test_caregiver_no_puede_borrar_entrada_de_otro(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care_a = make_user("cA@test.cl")
        care_b = make_user("cB@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care_a.user_id, "caregiver")
        add_member(pid, care_b.user_id, "caregiver")
        eid = self._crear_entry(care_a, pid)

        care_b.sql("delete from log_entries where id = %s", (eid,))
        assert care_a.sql("select count(*) from log_entries where id = %s", (eid,))[0][0] == 1


# ---------------------------------------------------------------------------
# Historial del asistente: RLS aísla por grupo (regla sagrada #7)
# ---------------------------------------------------------------------------


class TestHistorialAsistente:
    def _insert_msg(self, sess, patient_id: str, pregunta: str, cited: list[str]):
        return sess.sql(
            "insert into assistant_messages "
            "(patient_id, author_id, pregunta, respuesta, cited_entry_ids) "
            "values (%s, auth.uid(), %s, 'r', %s) returning id",
            (patient_id, pregunta, cited),
        )[0][0]

    def test_B_no_ve_historial_de_paciente_de_A(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        pid = _crear_paciente(a, "Doña Elba")
        self._insert_msg(a, pid, "¿cómo está?", [])

        assert a.sql("select count(*) from assistant_messages")[0][0] == 1
        assert b.sql("select count(*) from assistant_messages")[0][0] == 0

    def test_cited_entry_ids_se_persiste_como_uuid_array(self, make_user):
        a = make_user("a@test.cl")
        pid = _crear_paciente(a, "Doña Elba")
        # inserto una entry y capturo su id
        eid = a.sql(
            "insert into log_entries (patient_id, kind, content, occurred_at) "
            "values (%s, 'nota', 'x', now()) returning id",
            (pid,),
        )[0][0]
        self._insert_msg(a, pid, "¿?", [str(eid)])
        row = a.sql("select cited_entry_ids from assistant_messages")[0][0]
        assert len(row) == 1 and str(row[0]) == str(eid)


# ---------------------------------------------------------------------------
# Reglas sagradas de CLAUDE.md verificadas en la base
# ---------------------------------------------------------------------------


class TestReglasSagradas:
    def test_author_id_no_puede_falsificarse(self, make_user, add_member):
        """Regla #5: toda entrada de bitácora registra author_id = quien escribió."""
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")

        # care intenta insertar con author_id = admin (suplantación)
        # El WITH CHECK de entries_insert exige author_id = auth.uid();
        # Postgres reporta esto como InsufficientPrivilege (SQLSTATE 42501).
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
            care.sql(
                "insert into log_entries (patient_id, author_id, kind, content, occurred_at) "
                "values (%s, %s, 'nota', 'suplantación', now())",
                (pid, str(admin.user_id)),
            )
        assert "row-level security" in str(exc.value).lower()

    def test_cited_entry_ids_persiste_por_default(self, make_user, add_member):
        """Regla #4: cited_entry_ids es NOT NULL con default; siempre queda registro."""
        admin = make_user("admin@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")

        admin.sql(
            "insert into assistant_messages (patient_id, author_id, pregunta, respuesta) "
            "values (%s, auth.uid(), 'p', 'r')",
            (pid,),
        )
        row = admin.sql("select cited_entry_ids from assistant_messages")[0][0]
        assert row == []  # default '{}'::uuid[]
