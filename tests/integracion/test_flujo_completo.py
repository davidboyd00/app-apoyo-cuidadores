"""Integración real: HTTP → FastAPI → PostgREST → Postgres con RLS (EDT 5.1.1).

Recorre el flujo del MVP con dos familiares (Ana admin, Beto cuidador) y una
persona ajena (Carla). Además prueba concurrencia: reintentos simultáneos de
la cola offline con el mismo client_id no duplican registros.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.config import settings

pytestmark = pytest.mark.integracion


def _ahora() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(scope="module")
def familia(stack):
    ana = stack.persona("ana@familia.cl")
    beto = stack.persona("beto@familia.cl")
    carla = stack.persona("carla@otra.cl")
    with stack.cliente() as c:
        for p in (ana, beto, carla):
            r = c.post("/me/consent", json={"version": settings.policy_version}, headers=p.headers)
            assert r.status_code == 201, r.text
        r = c.post("/patients", json={"nombre": "Elba Soto"}, headers=ana.headers)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        r = c.post(
            f"/patients/{pid}/members",
            json={"email": beto.email, "role": "caregiver"},
            headers=ana.headers,
        )
        assert r.status_code == 201, r.text
    return {"stack": stack, "ana": ana, "beto": beto, "carla": carla, "pid": pid}


def _entrada(contenido: str, kind: str = "nota", **extra) -> dict:
    return {"kind": kind, "content": contenido, "occurred_at": _ahora().isoformat(), **extra}


def test_sin_consentimiento_el_negocio_responde_409(familia):
    stack = familia["stack"]
    nueva = stack.persona("sin.consentimiento@test.cl")
    with stack.cliente() as c:
        r = c.get("/patients", headers=nueva.headers)
    assert r.status_code == 409
    assert "política de privacidad" in r.json()["detail"]


def test_registro_offline_concurrente_no_duplica(familia):
    """Cinco reintentos simultáneos con el mismo client_id → una sola fila."""
    ana, pid, stack = familia["ana"], familia["pid"], familia["stack"]
    client_id = str(uuid4())
    cuerpo = _entrada(
        "Tomó losartán 50 mg en la mañana",
        "medicamento",
        client_id=client_id,
        registrado_en=(_ahora() - timedelta(minutes=20)).isoformat(),
    )

    async def enviar_cinco():
        async with httpx.AsyncClient(base_url=stack.api, timeout=30) as c:
            return await asyncio.gather(
                *[
                    c.post(f"/patients/{pid}/entries", json=cuerpo, headers=ana.headers)
                    for _ in range(5)
                ]
            )

    respuestas = asyncio.run(enviar_cinco())
    assert sorted(r.status_code for r in respuestas).count(201) == 1, [r.text for r in respuestas]
    assert {r.status_code for r in respuestas} <= {200, 201}
    assert len({r.json()["id"] for r in respuestas}) == 1
    familia["entrada_ana"] = respuestas[0].json()["id"]
    with stack.cliente() as c:
        feed = c.get(f"/patients/{pid}/entries", headers=ana.headers).json()
    assert [e["content"] for e in feed].count("Tomó losartán 50 mg en la mañana") == 1


def test_relevo_familiar_con_dos_autores(familia):
    beto, ana, pid, stack = familia["beto"], familia["ana"], familia["pid"], familia["stack"]
    with stack.cliente() as c:
        r = c.post(
            f"/patients/{pid}/entries",
            json=_entrada("Almorzó completo", "alimentacion", source="voice"),
            headers=beto.headers,
        )
        assert r.status_code == 201, r.text
        assert r.json()["author_id"] == beto.id
        assert r.json()["source"] == "voice"
        familia["entrada_beto"] = r.json()["id"]
        m = c.get(f"/patients/{pid}/metrics/relevo", headers=ana.headers).json()
    assert m["autores_distintos"] == 2
    assert m["cumple_kpi"] is True


def test_correccion_con_historial_y_permisos(familia):
    ana, beto, pid, stack = familia["ana"], familia["beto"], familia["pid"], familia["stack"]
    eid = familia["entrada_ana"]
    with stack.cliente() as c:
        # Beto no puede corregir lo que escribió Ana.
        r = c.post(
            f"/patients/{pid}/entries/{eid}/revisiones",
            json={**_entrada("Hackeo", "medicamento"), "motivo": "porque sí"},
            headers=beto.headers,
        )
        assert r.status_code in (403, 404), r.text
        # Ana sí, con motivo.
        r = c.post(
            f"/patients/{pid}/entries/{eid}/revisiones",
            json={
                **_entrada("Tomó losartán 100 mg en la mañana", "medicamento"),
                "motivo": "Dosis mal anotada",
            },
            headers=ana.headers,
        )
        assert r.status_code == 201, r.text
        assert r.json()["editada"] is True
        h = c.get(f"/patients/{pid}/entries/{eid}/historial", headers=beto.headers).json()
    assert h["original"]["content"] == "Tomó losartán 50 mg en la mañana"
    assert [(x["accion"], x["motivo"]) for x in h["revisiones"]] == [
        ("editar", "Dosis mal anotada")
    ]
    assert h["revisiones"][0]["author_id"] == ana.id


def test_anulacion_oculta_pero_conserva(familia):
    beto, pid, stack = familia["beto"], familia["pid"], familia["stack"]
    eid = familia["entrada_beto"]
    with stack.cliente() as c:
        r = c.post(
            f"/patients/{pid}/entries/{eid}/anulacion",
            json={"motivo": "Era de otra persona"},
            headers=beto.headers,
        )
        assert r.status_code == 201, r.text
        visibles = c.get(f"/patients/{pid}/entries", headers=beto.headers).json()
        todas = c.get(
            f"/patients/{pid}/entries", params={"incluir_anuladas": True}, headers=beto.headers
        ).json()
        # Una anulación es terminal.
        r2 = c.post(
            f"/patients/{pid}/entries/{eid}/anulacion",
            json={"motivo": "De nuevo"},
            headers=beto.headers,
        )
    assert eid not in [e["id"] for e in visibles]
    assert [e["anulada"] for e in todas if e["id"] == eid] == [True]
    assert r2.status_code >= 400


def test_persona_ajena_no_ve_nada(familia):
    carla, pid, stack = familia["carla"], familia["pid"], familia["stack"]
    with stack.cliente() as c:
        assert c.get("/patients", headers=carla.headers).json() == []
        assert c.get(f"/patients/{pid}/entries", headers=carla.headers).json() == []
        assert c.get(f"/patients/{pid}/turnos", headers=carla.headers).json() == []
        r = c.post(f"/patients/{pid}/entries", json=_entrada("Intrusa"), headers=carla.headers)
    assert r.status_code == 403


def test_asistente_cita_solo_entradas_reales(familia):
    ana, pid, stack = familia["ana"], familia["pid"], familia["stack"]
    with stack.cliente() as c:
        r = c.post(
            f"/patients/{pid}/assistant/ask",
            json={"pregunta": "¿Cómo estuvo esta semana?", "ventana_dias": 7},
            headers=ana.headers,
        )
        assert r.status_code == 200, r.text
        feed_ids = {e["id"] for e in c.get(f"/patients/{pid}/entries", headers=ana.headers).json()}
        m = c.get(f"/patients/{pid}/metrics/asistente", headers=ana.headers).json()
        historial = c.get(f"/patients/{pid}/assistant/messages", headers=ana.headers).json()
    citas = set(r.json()["cited_entry_ids"])
    assert citas and citas <= feed_ids  # la cita inventada por el LLM falso se descartó
    assert "00000000-0000-0000-0000-00000000dead" not in citas
    assert r.json()["suficiente"] is True
    assert m["total_respuestas"] == 1 and m["cumple_kpi"] is True
    assert historial[0]["suficiente"] is True


def test_resumen_asincrono_llega_a_listo(familia):
    ana, pid, stack = familia["ana"], familia["pid"], familia["stack"]
    with stack.cliente() as c:
        r = c.post(f"/patients/{pid}/summaries", params={"dias": 7}, headers=ana.headers)
        assert r.status_code == 202, r.text
        sid = r.json()["id"]
        assert r.json()["estado"] == "generando"
        for _ in range(50):
            s = c.get(f"/patients/{pid}/summaries/{sid}", headers=ana.headers).json()
            if s["estado"] != "generando":
                break
            time.sleep(0.1)
        # Pedirlo de nuevo reutiliza el reciente (200, sin llamar al LLM).
        r2 = c.post(f"/patients/{pid}/summaries", params={"dias": 7}, headers=ana.headers)
    assert s["estado"] == "listo", s
    assert "Trazabilidad del registro" in s["contenido"]
    assert "Corregidas: 1" in s["contenido"]
    assert r2.status_code == 200 and r2.json()["id"] == sid


def test_turnos_y_relevo(familia):
    ana, beto, pid, stack = familia["ana"], familia["beto"], familia["pid"], familia["stack"]
    inicio = _ahora().replace(microsecond=0)
    with stack.cliente() as c:
        r = c.post(
            f"/patients/{pid}/turnos",
            json={
                "cuidador_id": beto.id,
                "inicio": inicio.isoformat(),
                "fin": (inicio + timedelta(hours=8)).isoformat(),
            },
            headers=ana.headers,
        )
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        # Beto (cuidador) no puede agendar a Ana.
        r_no = c.post(
            f"/patients/{pid}/turnos",
            json={
                "cuidador_id": ana.id,
                "inicio": inicio.isoformat(),
                "fin": (inicio + timedelta(hours=2)).isoformat(),
            },
            headers=beto.headers,
        )
        lista = c.get(f"/patients/{pid}/turnos", headers=beto.headers).json()
        r_del = c.delete(f"/patients/{pid}/turnos/{tid}", headers=beto.headers)
        vacia = c.get(f"/patients/{pid}/turnos", headers=ana.headers).json()
    assert r_no.status_code == 403
    assert [t["id"] for t in lista] == [tid]
    assert r_del.status_code == 204
    assert vacia == []


def test_medicamentos_y_dosis_offline(familia):
    ana, pid, stack = familia["ana"], familia["pid"], familia["stack"]
    with stack.cliente() as c:
        r = c.post(
            f"/patients/{pid}/medications",
            json={"nombre": "Losartán", "dosis": "50 mg", "horarios": ["08:00:00", "20:00:00"]},
            headers=ana.headers,
        )
        assert r.status_code == 201, r.text
        mid = r.json()["id"]
        dosis = {
            "scheduled_for": _ahora().replace(minute=0, second=0, microsecond=0).isoformat(),
            "status": "tomada",
            "taken_at": _ahora().isoformat(),
            "client_id": str(uuid4()),
        }
        r1 = c.post(f"/patients/{pid}/medications/{mid}/doses", json=dosis, headers=ana.headers)
        r2 = c.post(f"/patients/{pid}/medications/{mid}/doses", json=dosis, headers=ana.headers)
        proximas = c.get(f"/patients/{pid}/medications/upcoming", headers=ana.headers)
        adherencia = c.get(
            f"/patients/{pid}/medications/adherence",
            params={
                "desde": (_ahora() - timedelta(days=1)).isoformat(),
                "hasta": (_ahora() + timedelta(days=1)).isoformat(),
            },
            headers=ana.headers,
        ).json()
    assert (r1.status_code, r2.status_code) == (201, 200)
    assert r1.json()["id"] == r2.json()["id"]
    assert proximas.status_code == 200 and proximas.json()
    assert adherencia["tomadas"] == 1


def test_exportacion_arco_y_eliminacion_de_cuenta(familia):
    ana, beto, pid, stack = familia["ana"], familia["beto"], familia["pid"], familia["stack"]
    inicio = _ahora().replace(microsecond=0) + timedelta(days=1)
    with stack.cliente() as c:
        c.post(
            f"/patients/{pid}/turnos",
            json={"inicio": inicio.isoformat(), "fin": (inicio + timedelta(hours=4)).isoformat()},
            headers=beto.headers,
        )
        export = c.get("/me/data-export", headers=beto.headers).json()
        assert export["usuario"]["id"] == beto.id
        assert [e["content"] for e in export["entradas_bitacora"]] == ["Almorzó completo"]
        assert [r["accion"] for r in export["revisiones_bitacora"]] == ["anular"]
        assert len(export["turnos"]) == 1

        # Sin confirmación explícita no borra nada.
        assert c.delete("/me", headers=beto.headers).status_code == 400
        r = c.delete("/me", params={"confirmar": True}, headers=beto.headers)
        assert r.status_code == 204, r.text
        todas = c.get(
            f"/patients/{pid}/entries", params={"incluir_anuladas": True}, headers=ana.headers
        ).json()
        turnos = c.get(f"/patients/{pid}/turnos", headers=ana.headers).json()
    # La bitácora se conserva con autoría anonimizada; sus turnos se eliminan.
    autores = {e["author_id"] for e in todas}
    assert beto.id not in autores
    assert "00000000-0000-0000-0000-000000000000" in autores
    assert turnos == []


def test_centros_busqueda_por_region_tipo_y_comuna(familia):
    """Centros es público y son miles: región, tipo y texto se filtran en el
    servidor, y el texto encuentra la comuna aunque se escriba sin tildes."""
    from tests.conftest import _connect

    stack = familia["stack"]
    with _connect(stack.db) as conn, conn.cursor() as cur:
        cur.executemany(
            "insert into centers (nombre, tipo, region, comuna, direccion, telefono) "
            "values (%s, %s, %s, %s, %s, %s)",
            [
                (
                    "Farmacia Prueba Centro",
                    "Farmacia",
                    "Biobío",
                    "Concepción",
                    "Barros Arana 1",
                    None,
                ),
                (
                    "Farmacia Prueba Turno",
                    "Farmacia de turno",
                    "Biobío",
                    "Concepción",
                    None,
                    "+56412345678",
                ),
                ("CESFAM Prueba Ñuñoa", "CESFAM", "Metropolitana", "Ñuñoa", "Irarrázaval 2", None),
            ],
        )
    with stack.cliente() as c:
        r = c.get("/centers", params={"region": "Biobío", "q": "concepcion"})
        assert r.status_code == 200, r.text
        assert {x["nombre"] for x in r.json()} == {
            "Farmacia Prueba Centro",
            "Farmacia Prueba Turno",
        }
        r = c.get("/centers", params={"region": "Biobío", "tipo": "Farmacia de turno"})
        assert [x["nombre"] for x in r.json()] == ["Farmacia Prueba Turno"]
        r = c.get("/centers", params={"q": "ñuñoa"})
        assert [x["nombre"] for x in r.json()] == ["CESFAM Prueba Ñuñoa"]
        # Signos que romperían el filtro de PostgREST se ignoran.
        r = c.get("/centers", params={"q": "barros arana, (1)"})
        assert r.status_code == 200
        assert [x["nombre"] for x in r.json()] == ["Farmacia Prueba Centro"]
