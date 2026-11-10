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


class TestHistorialInmutable:
    """La bitácora es insert-only: ni autor ni admin modifican o borran el
    registro original. Las correcciones y anulaciones son revisiones
    append-only con autor, hora y motivo (Ley 21.719: integridad y
    responsabilidad; validez probatoria del resumen médico)."""

    def _crear_entry(self, sess, patient_id: str, texto: str = "original") -> str:
        return sess.sql(
            "insert into log_entries (patient_id, kind, content, occurred_at) "
            "values (%s, 'nota', %s, now()) returning id",
            (patient_id, texto),
        )[0][0]

    def _revisar(self, sess, eid, pid, content="corregido", motivo="error de tipeo"):
        sess.sql(
            "insert into log_entry_revisions "
            "(entry_id, patient_id, accion, motivo, kind, content, occurred_at) "
            "values (%s, %s, 'editar', %s, 'nota', %s, now())",
            (eid, pid, motivo, content),
        )

    def _anular(self, sess, eid, pid, motivo="registrado en paciente equivocado"):
        sess.sql(
            "insert into log_entry_revisions (entry_id, patient_id, accion, motivo) "
            "values (%s, %s, 'anular', %s)",
            (eid, pid, motivo),
        )

    # --- Insert-only ------------------------------------------------------

    def test_autor_no_puede_modificar_su_original(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid, "original")

        care.sql("update log_entries set content = 'hack' where id = %s", (eid,))
        assert care.sql("select content from log_entries where id = %s", (eid,))[0][0] == "original"

    def test_admin_no_puede_modificar_entrada_ajena(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid, "original")

        admin.sql("update log_entries set content = 'admin' where id = %s", (eid,))
        assert (
            admin.sql("select content from log_entries where id = %s", (eid,))[0][0] == "original"
        )

    def test_nadie_puede_borrar_un_registro_original(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid)

        care.sql("delete from log_entries where id = %s", (eid,))
        admin.sql("delete from log_entries where id = %s", (eid,))
        assert admin.sql("select count(*) from log_entries where id = %s", (eid,))[0][0] == 1

    # --- Revisiones -------------------------------------------------------

    def test_autor_revisa_su_entrada_y_el_original_queda_intacto(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid, "original")

        self._revisar(care, eid, pid, content="corregido")

        vigente = care.sql(
            "select content, editada, anulada from log_entries_vigentes where id = %s", (eid,)
        )[0]
        assert vigente == ("corregido", True, False)
        # El registro original no cambió: la evidencia sigue ahí.
        assert care.sql("select content from log_entries where id = %s", (eid,))[0][0] == "original"

    def test_admin_revisa_entrada_ajena_y_queda_su_autoria(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid)

        self._revisar(admin, eid, pid, content="admin corrigió")

        autor = admin.sql("select author_id from log_entry_revisions where entry_id = %s", (eid,))[
            0
        ][0]
        assert str(autor) == str(admin.user_id)

    def test_caregiver_no_revisa_entrada_de_otro(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care_a = make_user("cA@test.cl")
        care_b = make_user("cB@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care_a.user_id, "caregiver")
        add_member(pid, care_b.user_id, "caregiver")
        eid = self._crear_entry(care_a, pid, "de A")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            self._revisar(care_b, eid, pid, content="hack")
        assert (
            care_a.sql("select content from log_entries_vigentes where id = %s", (eid,))[0][0]
            == "de A"
        )

    def test_viewer_no_revisa(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        viewer = make_user("viewer@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        add_member(pid, viewer.user_id, "viewer")
        eid = self._crear_entry(care, pid)

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            self._revisar(viewer, eid, pid)

    def test_revision_no_puede_falsificar_autoria(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid)

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            care.sql(
                "insert into log_entry_revisions "
                "(entry_id, patient_id, author_id, accion, motivo, kind, content, occurred_at) "
                "values (%s, %s, %s, 'editar', 'suplantación', 'nota', 'x', now())",
                (eid, pid, str(admin.user_id)),
            )

    def test_revision_no_puede_apuntar_a_entrada_de_otro_paciente(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        p1 = _crear_paciente(admin, "Paciente 1")
        p2 = _crear_paciente(admin, "Paciente 2")
        add_member(p1, care.user_id, "caregiver")
        add_member(p2, care.user_id, "caregiver")
        eid = self._crear_entry(care, p1)

        # Entrada de p1 "revisada" bajo p2: el helper exige que coincidan.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            self._revisar(care, eid, p2)

    def test_motivo_de_solo_espacios_es_rechazado(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid)

        with pytest.raises(psycopg.errors.CheckViolation):
            self._revisar(care, eid, pid, motivo="   ")

    def test_editar_exige_estado_nuevo_completo(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid)

        with pytest.raises(psycopg.errors.CheckViolation):
            care.sql(
                "insert into log_entry_revisions (entry_id, patient_id, accion, motivo) "
                "values (%s, %s, 'editar', 'sin contenido nuevo')",
                (eid, pid),
            )

    def test_historial_inmutable_ni_autor_ni_admin_lo_modifican(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid)
        self._revisar(care, eid, pid)

        care.sql("update log_entry_revisions set motivo = 'reescrito' where entry_id = %s", (eid,))
        care.sql("delete from log_entry_revisions where entry_id = %s", (eid,))
        admin.sql("delete from log_entry_revisions where entry_id = %s", (eid,))
        assert (
            admin.sql("select count(*) from log_entry_revisions where entry_id = %s", (eid,))[0][0]
            == 1
        )

    def test_miembro_lee_el_historial_completo(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        viewer = make_user("viewer@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        add_member(pid, viewer.user_id, "viewer")
        eid = self._crear_entry(care, pid)
        self._revisar(care, eid, pid, content="v2")
        self._revisar(care, eid, pid, content="v3")

        assert (
            viewer.sql("select count(*) from log_entry_revisions where entry_id = %s", (eid,))[0][0]
            == 2
        )

    def test_no_miembro_no_ve_historial_ni_vista(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        ajeno = make_user("ajeno@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid)
        self._revisar(care, eid, pid)

        assert ajeno.sql("select count(*) from log_entry_revisions")[0][0] == 0
        assert ajeno.sql("select count(*) from log_entries_vigentes")[0][0] == 0

    # --- Anulación --------------------------------------------------------

    def test_anulacion_marca_la_entrada_sin_borrarla(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid, "dosis registrada por error")

        self._anular(care, eid, pid)

        vigente = care.sql(
            "select content, anulada from log_entries_vigentes where id = %s", (eid,)
        )[0]
        assert vigente == ("dosis registrada por error", True)
        assert care.sql("select count(*) from log_entries where id = %s", (eid,))[0][0] == 1

    def test_anulacion_es_terminal(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        eid = self._crear_entry(care, pid)
        self._anular(care, eid, pid)

        # La política (not entrada_anulada) bloquea antes que el índice único:
        # ni el autor ni el admin pueden anular de nuevo ni reabrir la entrada.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            self._anular(admin, eid, pid, motivo="segunda anulación")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            self._revisar(care, eid, pid, content="intento de reabrir")

    def test_vista_respeta_rls_entre_grupos(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        ajeno = make_user("ajeno@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        self._crear_entry(admin, pid)

        assert ajeno.sql("select count(*) from log_entries_vigentes")[0][0] == 0

    # --- Idempotencia offline ---------------------------------------------

    def test_client_id_es_unico_por_autor(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        cid = "11111111-1111-1111-1111-111111111111"
        care.sql(
            "insert into log_entries (patient_id, kind, content, occurred_at, client_id) "
            "values (%s, 'nota', 'offline', now(), %s)",
            (pid, cid),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            care.sql(
                "insert into log_entries (patient_id, kind, content, occurred_at, client_id) "
                "values (%s, 'nota', 'reintento', now(), %s)",
                (pid, cid),
            )

    def test_client_id_puede_repetirse_entre_autores(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        add_member(pid, care.user_id, "caregiver")
        cid = "22222222-2222-2222-2222-222222222222"
        admin.sql(
            "insert into log_entries (patient_id, kind, content, occurred_at, client_id) "
            "values (%s, 'nota', 'a', now(), %s)",
            (pid, cid),
        )
        care.sql(
            "insert into log_entries (patient_id, kind, content, occurred_at, client_id) "
            "values (%s, 'nota', 'b', now(), %s)",
            (pid, cid),
        )
        assert admin.sql("select count(*) from log_entries where client_id = %s", (cid,))[0][0] == 2


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
