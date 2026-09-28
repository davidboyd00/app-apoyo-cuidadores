"""Tests de la tabla medication_doses y del cálculo de próximas dosis.

Cubren:
    - RLS: aislamiento entre grupos y roles (viewer no escribe).
    - Suplantación de author_id bloqueada (regla sagrada #5).
    - Append-only: sin policies UPDATE/DELETE (updates/deletes no afectan filas).
    - CHECK constraint status/taken_at.
    - Cálculo puro de /upcoming (proyectar_upcoming): ventana y ordenamiento.
"""

from __future__ import annotations

from datetime import datetime, time
from uuid import uuid4

import psycopg
import pytest

from app.routers.medications import TZ_CHILE, proyectar_upcoming

pytestmark = pytest.mark.rls


def _crear_paciente(sess, nombre: str) -> str:
    return sess.sql("insert into patients (nombre) values (%s) returning id", (nombre,))[0][0]


def _crear_med(sess, patient_id: str, nombre: str = "paracetamol") -> str:
    return sess.sql(
        "insert into medications (patient_id, nombre) values (%s, %s) returning id",
        (patient_id, nombre),
    )[0][0]


def _insert_dose(
    sess,
    patient_id: str,
    medication_id: str,
    status: str = "tomada",
    scheduled_for: datetime | None = None,
    taken_at: datetime | None = None,
) -> str:
    if scheduled_for is None:
        scheduled_for = datetime.now(tz=TZ_CHILE)
    if status == "tomada" and taken_at is None:
        taken_at = scheduled_for
    return sess.sql(
        "insert into medication_doses "
        "(patient_id, medication_id, scheduled_for, taken_at, status) "
        "values (%s, %s, %s, %s, %s) returning id",
        (patient_id, medication_id, scheduled_for, taken_at, status),
    )[0][0]


# ---------------------------------------------------------------------------
# RLS: aislamiento y roles
# ---------------------------------------------------------------------------


class TestAislamiento:
    def test_B_no_ve_dosis_de_paciente_de_A(self, make_user):
        a = make_user("a@test.cl")
        b = make_user("b@test.cl")
        pid = _crear_paciente(a, "Doña Elba")
        mid = _crear_med(a, pid)
        _insert_dose(a, pid, mid)

        assert a.sql("select count(*) from medication_doses")[0][0] == 1
        assert b.sql("select count(*) from medication_doses")[0][0] == 0

    def test_viewer_no_puede_registrar_dosis(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        viewer = make_user("viewer@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        mid = _crear_med(admin, pid)
        add_member(pid, viewer.user_id, "viewer")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _insert_dose(viewer, pid, mid)

    def test_caregiver_puede_registrar_dosis(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        mid = _crear_med(admin, pid)
        add_member(pid, care.user_id, "caregiver")

        _insert_dose(care, pid, mid)
        assert care.sql("select count(*) from medication_doses")[0][0] == 1


# ---------------------------------------------------------------------------
# Regla sagrada #5: author_id refleja quién marcó la dosis (KPI relevo familiar)
# ---------------------------------------------------------------------------


class TestAuthorId:
    def test_author_id_se_pone_por_default_a_auth_uid(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        mid = _crear_med(admin, pid)
        add_member(pid, care.user_id, "caregiver")

        _insert_dose(care, pid, mid)
        row = care.sql("select author_id from medication_doses")[0][0]
        assert str(row) == str(care.user_id)

    def test_no_se_puede_falsificar_author_id(self, make_user, add_member):
        """Care intenta insertar con author_id = admin → RLS bloquea (regla #5)."""
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        mid = _crear_med(admin, pid)
        add_member(pid, care.user_id, "caregiver")

        with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
            care.sql(
                "insert into medication_doses "
                "(patient_id, medication_id, author_id, scheduled_for, taken_at, status) "
                "values (%s, %s, %s, now(), now(), 'tomada')",
                (pid, mid, str(admin.user_id)),
            )
        assert "row-level security" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Append-only por diseño (sin policies UPDATE/DELETE)
# ---------------------------------------------------------------------------


class TestAppendOnly:
    def test_update_no_afecta_filas(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        mid = _crear_med(admin, pid)
        add_member(pid, care.user_id, "caregiver")
        did = _insert_dose(care, pid, mid)

        # Sin policy UPDATE → RLS filtra USING como false → 0 filas afectadas.
        care.sql("update medication_doses set status = 'omitida' where id = %s", (did,))
        row = care.sql("select status from medication_doses where id = %s", (did,))[0]
        assert row[0] == "tomada"

    def test_delete_no_afecta_filas(self, make_user, add_member):
        admin = make_user("admin@test.cl")
        care = make_user("care@test.cl")
        pid = _crear_paciente(admin, "Doña Elba")
        mid = _crear_med(admin, pid)
        add_member(pid, care.user_id, "caregiver")
        _insert_dose(care, pid, mid)

        care.sql("delete from medication_doses")
        assert care.sql("select count(*) from medication_doses")[0][0] == 1


# ---------------------------------------------------------------------------
# CHECK constraint status/taken_at
# ---------------------------------------------------------------------------


class TestCoherenciaStatus:
    def test_tomada_sin_taken_at_falla(self, make_user):
        a = make_user("a@test.cl")
        pid = _crear_paciente(a, "Doña Elba")
        mid = _crear_med(a, pid)

        with pytest.raises(psycopg.errors.CheckViolation):
            a.sql(
                "insert into medication_doses "
                "(patient_id, medication_id, scheduled_for, status) "
                "values (%s, %s, now(), 'tomada')",
                (pid, mid),
            )

    def test_omitida_con_taken_at_falla(self, make_user):
        a = make_user("a@test.cl")
        pid = _crear_paciente(a, "Doña Elba")
        mid = _crear_med(a, pid)

        with pytest.raises(psycopg.errors.CheckViolation):
            a.sql(
                "insert into medication_doses "
                "(patient_id, medication_id, scheduled_for, taken_at, status) "
                "values (%s, %s, now(), now(), 'omitida')",
                (pid, mid),
            )


# ---------------------------------------------------------------------------
# Cálculo puro de /upcoming (sin BD)
# ---------------------------------------------------------------------------


class TestProyectarUpcoming:
    MED_ID = uuid4()

    def _med(self, horarios: list[time]) -> dict:
        return {
            "id": str(self.MED_ID),
            "nombre": "paracetamol",
            "dosis": "500mg",
            "horarios": horarios,
        }

    def test_horario_futuro_del_dia_aparece_una_vez(self):
        ahora = datetime(2026, 10, 1, 6, 0, tzinfo=TZ_CHILE)
        meds = [self._med([time(8, 0)])]
        res = proyectar_upcoming(meds, ahora, within_hours=6)
        assert len(res) == 1
        assert res[0]["scheduled_for"] == datetime(2026, 10, 1, 8, 0, tzinfo=TZ_CHILE)

    def test_horario_pasado_salta_a_mañana(self):
        # A las 14:00 ya pasó el horario 08:00; con ventana 24h se agenda para
        # mañana a las 08:00 (queda dentro de la ventana).
        ahora = datetime(2026, 10, 1, 14, 0, tzinfo=TZ_CHILE)
        meds = [self._med([time(8, 0)])]
        res = proyectar_upcoming(meds, ahora, within_hours=24)
        assert len(res) == 1
        assert res[0]["scheduled_for"] == datetime(2026, 10, 2, 8, 0, tzinfo=TZ_CHILE)

    def test_ventana_larga_repite_horarios_por_dia(self):
        # 08:00 diario, ventana 48h desde 07:00 → hoy 08:00 y mañana 08:00.
        ahora = datetime(2026, 10, 1, 7, 0, tzinfo=TZ_CHILE)
        meds = [self._med([time(8, 0)])]
        res = proyectar_upcoming(meds, ahora, within_hours=48)
        scheduled = [r["scheduled_for"] for r in res]
        assert scheduled == [
            datetime(2026, 10, 1, 8, 0, tzinfo=TZ_CHILE),
            datetime(2026, 10, 2, 8, 0, tzinfo=TZ_CHILE),
        ]

    def test_multiples_horarios_ordenados(self):
        ahora = datetime(2026, 10, 1, 6, 0, tzinfo=TZ_CHILE)
        meds = [self._med([time(20, 0), time(8, 0), time(14, 0)])]
        res = proyectar_upcoming(meds, ahora, within_hours=24)
        horas = [r["scheduled_for"].hour for r in res]
        assert horas == sorted(horas)

    def test_med_sin_horarios_no_proyecta(self):
        # frecuencia_horas sin horarios: se ignora en MVP (TODO documentado).
        ahora = datetime(2026, 10, 1, 6, 0, tzinfo=TZ_CHILE)
        meds = [self._med([])]
        assert proyectar_upcoming(meds, ahora, within_hours=48) == []

    def test_horarios_como_string_iso(self):
        # Supabase serializa `time` como string ("08:00:00"). Debe parsearse.
        ahora = datetime(2026, 10, 1, 6, 0, tzinfo=TZ_CHILE)
        meds = [{"id": str(uuid4()), "nombre": "x", "horarios": ["08:00:00"]}]
        res = proyectar_upcoming(meds, ahora, within_hours=6)
        assert len(res) == 1
        assert res[0]["scheduled_for"].hour == 8
