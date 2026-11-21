"""Demo local con datos FICTICIOS sobre el stack real (sin Supabase ni IA real).

    POSTGREST_BIN=/ruta/postgrest python -m tests.integracion.demo [--salida demo.json]

Levanta Postgres + PostgREST + API (LLM simulado) en puertos fijos, carga una
familia de ejemplo por la API (como lo haría la app) y deja todo corriendo
hasta Ctrl+C. Escribe en `--salida` las URLs y los tokens de las personas de
ejemplo, para abrir la web local o automatizar pruebas de la interfaz.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

from app.config import settings
from tests.conftest import _connect
from tests.integracion.stack import levantar_stack

PUERTO_API = 8001
PUERTO_SUPABASE = 54321


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def sembrar(stack) -> dict:
    ahora = datetime.now(UTC).replace(second=0, microsecond=0)
    ana = stack.persona("ana.soto@ejemplo.cl")
    beto = stack.persona("beto.soto@ejemplo.cl")
    carmen = stack.persona("carmen.rojas@ejemplo.cl")
    with stack.cliente() as c:
        for p, nombre in ((ana, "Ana Soto"), (beto, "Beto Soto"), (carmen, "Carmen Rojas")):
            assert (
                c.post(
                    "/me/consent", json={"version": settings.policy_version}, headers=p.headers
                ).status_code
                == 201
            )
            c.put("/me/profile", json={"nombre": nombre}, headers=p.headers)
        pid = c.post(
            "/patients",
            json={
                "nombre": "Elba Soto",
                "fecha_nacimiento": "1941-03-12T00:00:00Z",
                "notas": "Hipertensa y diabética. Usa bastón. Datos ficticios de demostración.",
            },
            headers=ana.headers,
        ).json()["id"]
        c.post(
            f"/patients/{pid}/members",
            json={"email": beto.email, "role": "caregiver"},
            headers=ana.headers,
        )
        c.post(
            f"/patients/{pid}/members",
            json={"email": carmen.email, "role": "viewer"},
            headers=ana.headers,
        )

        def entrada(p, horas_atras, kind, content, source="text"):
            r = c.post(
                f"/patients/{pid}/entries",
                json={
                    "kind": kind,
                    "content": content,
                    "occurred_at": _iso(ahora - timedelta(hours=horas_atras)),
                    "source": source,
                },
                headers=p.headers,
            )
            assert r.status_code == 201, r.text
            return r.json()["id"]

        entrada(ana, 130, "ejercicio", "Caminata de 15 minutos por el jardín, con bastón.")
        entrada(
            beto,
            120,
            "sintoma",
            "Mareo leve al levantarse de la cama. Se sentó y pasó en unos minutos.",
        )
        entrada(
            ana,
            98,
            "animo",
            "Estuvo tranquila toda la tarde; conversó con su nieta por videollamada.",
        )
        entrada(
            beto,
            76,
            "alimentacion",
            "Almorzó completo: cazuela y fruta. Tomó dos vasos de agua.",
            "voice",
        )
        dosis = entrada(ana, 50, "medicamento", "Tomó losartán 50 mg con el desayuno.")
        entrada(beto, 30, "nota", "Durmió de corrido, se levantó una vez al baño.")
        entrada(ana, 26, "sintoma", "Presión 138/86 en la mañana, sin dolor de cabeza.")
        entrada(beto, 6, "alimentacion", "Desayuno: avena con plátano y té.", "voice")
        entrada(beto, 2, "animo", "Contenta: armó un rompecabezas conmigo.")
        # Una corrección con motivo, para mostrar el historial.
        c.post(
            f"/patients/{pid}/entries/{dosis}/revisiones",
            json={
                "kind": "medicamento",
                "content": "Tomó losartán 50 mg y metformina 850 mg con el desayuno.",
                "occurred_at": _iso(ahora - timedelta(hours=50)),
                "motivo": "Faltaba anotar la metformina",
            },
            headers=ana.headers,
        )

        losartan = c.post(
            f"/patients/{pid}/medications",
            json={
                "nombre": "Losartán",
                "dosis": "50 mg",
                "horarios": ["08:00:00", "20:00:00"],
                "notas": "con comida",
            },
            headers=ana.headers,
        ).json()["id"]
        c.post(
            f"/patients/{pid}/medications",
            json={"nombre": "Metformina", "dosis": "850 mg", "horarios": ["13:00:00"]},
            headers=ana.headers,
        )
        ayer = (ahora - timedelta(days=1)).astimezone(UTC)
        for p, h, estado in ((ana, 11, "tomada"), (beto, 23, "tomada")):
            c.post(
                f"/patients/{pid}/medications/{losartan}/doses",
                json={
                    "scheduled_for": _iso(ayer.replace(hour=h, minute=0)),
                    "status": estado,
                    "taken_at": _iso(ayer.replace(hour=h, minute=5)),
                },
                headers=p.headers,
            )

        # Turnos: Beto a cargo ahora, Ana mañana y un turno ya cumplido.
        for p, cuidador, ini, fin in (
            (ana, beto, ahora - timedelta(hours=3), ahora + timedelta(hours=5)),
            (ana, ana, ahora + timedelta(hours=14), ahora + timedelta(hours=26)),
            (ana, ana, ahora - timedelta(hours=30), ahora - timedelta(hours=20)),
        ):
            r = c.post(
                f"/patients/{pid}/turnos",
                json={"cuidador_id": cuidador.id, "inicio": _iso(ini), "fin": _iso(fin)},
                headers=p.headers,
            )
            assert r.status_code == 201, r.text

        c.post(
            f"/patients/{pid}/assistant/ask",
            json={"pregunta": "¿Cómo ha dormido esta semana?", "ventana_dias": 7},
            headers=ana.headers,
        )
        sid = c.post(f"/patients/{pid}/summaries", params={"dias": 7}, headers=ana.headers).json()[
            "id"
        ]
        for _ in range(50):
            if (
                c.get(f"/patients/{pid}/summaries/{sid}", headers=ana.headers).json()["estado"]
                != "generando"
            ):
                break
            time.sleep(0.1)

    # Centros de ejemplo (en producción los cargan los scrapers con service role).
    with _connect(stack.db) as conn, conn.cursor() as cur:
        cur.executemany(
            "insert into centers (nombre, tipo, region, comuna, direccion, telefono) values (%s, %s, %s, %s, %s, %s)",
            [
                (
                    "CESFAM Ejemplo Norte",
                    "Centro de salud",
                    "Metropolitana",
                    "Providencia",
                    "Av. Ejemplo 123",
                    "+56 2 2000 0001",
                ),
                (
                    "Farmacia de Turno Ejemplo",
                    "Farmacia",
                    "Metropolitana",
                    "Ñuñoa",
                    "Calle Demo 456",
                    "+56 2 2000 0002",
                ),
                (
                    "Hospital Ejemplo Sur",
                    "Hospital",
                    "Metropolitana",
                    "San Miguel",
                    "Gran Avenida 789",
                    "+56 2 2000 0003",
                ),
                (
                    "Farmacia Ejemplo Costa",
                    "Farmacia",
                    "Valparaíso",
                    "Viña del Mar",
                    "Av. Libertad 10",
                    None,
                ),
            ],
        )

    return {
        "api": stack.api,
        "supabase": stack.supabase,
        "anon_key": stack.anon_key,
        "paciente_id": pid,
        "personas": {
            n: {"id": p.id, "email": p.email, "token": p.token}
            for n, p in (("ana", ana), ("beto", beto), ("carmen", carmen))
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--salida", default="demo-local.json")
    args = parser.parse_args()
    binario = os.environ.get("POSTGREST_BIN") or shutil.which("postgrest")
    if not binario:
        print("Falta PostgREST: define POSTGREST_BIN", file=sys.stderr)
        return 2
    with levantar_stack(binario, puerto_api=PUERTO_API, puerto_supabase=PUERTO_SUPABASE) as stack:
        datos = sembrar(stack)
        with open(args.salida, "w") as f:
            json.dump(datos, f, indent=2)
        print(
            f"Demo lista: API {stack.api} · Supabase local {stack.supabase} · datos en {args.salida}",
            flush=True,
        )
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
