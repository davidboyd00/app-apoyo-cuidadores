"""Graba en video el asistente y el resumen médico usando la IA (EDT 5.3.2).

Corre contra la demo local con datos FICTICIOS (nunca datos reales):

    python -m tests.integracion.demo --llm-real --salida /tmp/demo.json
    # web compilada apuntando a la demo y servida en --web
    python scripts/video_asistente.py --demo /tmp/demo.json --web http://127.0.0.1:4173 --salida video

Recorre en formato celular: bitácora → dos preguntas al asistente (una con
datos y otra sin datos suficientes) → resumen de 30 días → vista para imprimir.
Deja `asistente.webm` y `resultado.json` (preguntas, respuestas y citas) en
`--salida`. Termina con código 1 si la IA no respondió, igual dejando el video.

El workflow "Video del asistente" lo corre en GitHub Actions con el Gemini
real (secret GEMINI_API_KEY).
"""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

# supabase-js guarda la sesión en sb-<primer segmento del host>-auth-token.
CLAVE_SESION = "sb-127-auth-token"
ANCHO, ALTO = 390, 844

PREGUNTAS = [
    # Con datos en la bitácora de la demo: debe citar registros.
    "¿Cómo ha estado su ánimo esta semana?",
    # Sin datos: el asistente debe decir que la bitácora no alcanza.
    "¿Cuánto pesa actualmente?",
]

ETIQUETA = """
window.addEventListener('DOMContentLoaded', () => {
  const b = document.createElement('div');
  b.textContent = 'Prueba con IA real · datos ficticios';
  b.style.cssText = 'position:fixed;bottom:10px;left:50%;transform:translateX(-50%);'
    + 'z-index:99999;background:rgba(17,24,39,.82);color:#fff;font:600 12px/1.4 system-ui;'
    + 'padding:5px 12px;border-radius:999px;pointer-events:none;white-space:nowrap';
  document.body.appendChild(b);
});
"""


def _sesion(persona: dict) -> str:
    payload = persona["token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return json.dumps(
        {
            "access_token": persona["token"],
            "refresh_token": "demo",
            "token_type": "bearer",
            "expires_in": claims["exp"] - int(time.time()),
            "expires_at": claims["exp"],
            "user": {
                "id": persona["id"],
                "email": persona["email"],
                "aud": "authenticated",
                "role": "authenticated",
                "app_metadata": {},
                "user_metadata": {},
                "created_at": "2026-01-01T00:00:00Z",
            },
        }
    )


def _pausa(page: Page, segundos: float) -> None:
    page.wait_for_timeout(int(segundos * 1000))


def _desplazar(page: Page, total: int, pasos: int = 6) -> None:
    for _ in range(pasos):
        page.mouse.wheel(0, total / pasos)
        _pausa(page, 0.35)


def preguntar(page: Page, pregunta: str) -> dict:
    caja = page.get_by_label("Tu pregunta")
    caja.click()
    caja.press_sequentially(pregunta, delay=45)
    _pausa(page, 0.6)
    inicio = time.monotonic()
    page.get_by_role("button", name="Preguntar", exact=True).click()
    tarjeta = page.locator("article").filter(has_text=pregunta).first
    alerta = page.get_by_role("form", name="Preguntar al asistente").get_by_role("alert")
    expect(tarjeta.or_(alerta)).to_be_visible(timeout=90_000)
    segundos = round(time.monotonic() - inicio, 1)
    if alerta.is_visible():
        return {"pregunta": pregunta, "error": alerta.inner_text(), "segundos": segundos}
    tarjeta.scroll_into_view_if_needed()
    return {
        "pregunta": pregunta,
        "respuesta": tarjeta.locator("p").nth(2).inner_text(),
        "muestra_registros": tarjeta.get_by_role("button", name="Ocultar registros").count() > 0,
        "dice_que_no_alcanza": tarjeta.get_by_text("La bitácora no alcanza para responder").count()
        > 0,
        "segundos": segundos,
    }


def trazabilidad(demo: dict) -> list[dict]:
    """Lo que guardó la API de cada pregunta (regla sagrada #4): citas validadas
    contra el contexto y si el modelo dijo que la bitácora alcanzaba."""
    persona = demo["personas"]["ana"]
    r = httpx.get(
        f"{demo['api']}/patients/{demo['paciente_id']}/assistant/messages",
        headers={"Authorization": f"Bearer {persona['token']}"},
        timeout=30,
    )
    r.raise_for_status()
    return [
        {
            "pregunta": m.get("pregunta"),
            "citas_validas": len(m.get("cited_entry_ids") or []),
            "suficiente": m.get("suficiente"),
        }
        for m in r.json()
        if m.get("pregunta") in PREGUNTAS
    ]


def grabar(demo: dict, web: str, salida: Path) -> dict:
    pid = demo["paciente_id"]
    resultado: dict = {"preguntas": [], "resumen": None}
    with sync_playwright() as p:
        browser = p.chromium.launch(slow_mo=120)
        ctx = browser.new_context(
            viewport={"width": ANCHO, "height": ALTO},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            locale="es-CL",
            timezone_id="America/Santiago",
            record_video_dir=str(salida),
            # Al doble de la resolución lógica: el texto se lee nítido en el celular.
            record_video_size={"width": ANCHO * 2, "height": ALTO * 2},
        )
        ctx.add_init_script(
            f"localStorage.setItem({json.dumps(CLAVE_SESION)}, {json.dumps(_sesion(demo['personas']['ana']))});"
        )
        ctx.add_init_script(ETIQUETA)
        page = ctx.new_page()
        try:
            # 1. La bitácora de la persona ficticia.
            page.goto(f"{web}/patients/{pid}")
            expect(page.get_by_role("heading", name="Bitácora")).to_be_visible(timeout=30_000)
            _pausa(page, 2.5)
            _desplazar(page, 900)
            _pausa(page, 1)

            # 2. El asistente responde solo con lo registrado y muestra en qué se basa.
            page.goto(f"{web}/patients/{pid}/asistente")
            expect(page.get_by_label("Tu pregunta")).to_be_visible(timeout=30_000)
            _pausa(page, 1.5)
            for pregunta in PREGUNTAS:
                r = preguntar(page, pregunta)
                resultado["preguntas"].append(r)
                print(json.dumps(r, ensure_ascii=False), flush=True)
                _pausa(page, 2)
                _desplazar(page, 500)
                _pausa(page, 3)
                page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
                _pausa(page, 1.2)

            # 3. Resumen para la consulta médica, generado en el momento.
            page.goto(f"{web}/patients/{pid}/resumenes")
            expect(page.get_by_role("button", name="Generar resumen")).to_be_visible(timeout=30_000)
            _pausa(page, 1.5)
            antes = page.get_by_role("link", name="Ver e imprimir").count()
            page.get_by_label("Período").select_option("30")
            _pausa(page, 0.8)
            inicio = time.monotonic()
            page.get_by_role("button", name="Generar resumen").click()
            expect(page.get_by_role("link", name="Ver e imprimir")).to_have_count(
                antes + 1, timeout=120_000
            )
            segundos = round(time.monotonic() - inicio, 1)
            _pausa(page, 1.5)
            page.get_by_role("link", name="Ver e imprimir").first.click()
            expect(page.get_by_role("heading", name="Resumen de cuidado")).to_be_visible(
                timeout=30_000
            )
            _pausa(page, 2.5)
            resultado["resumen"] = {
                "texto": page.locator("main").inner_text()[:6000],
                "segundos": segundos,
            }
            _desplazar(page, 1800, pasos=12)
            _pausa(page, 2.5)
        except Exception as exc:  # el video igual queda para ver dónde falló
            resultado["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
            _pausa(page, 2)
        finally:
            video = page.video
            ctx.close()
            browser.close()
            if video:
                Path(video.path()).rename(salida / "asistente.webm")
    return resultado


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", required=True, help="JSON que escribe tests.integracion.demo")
    parser.add_argument("--web", default="http://127.0.0.1:4173")
    parser.add_argument("--salida", default="video")
    args = parser.parse_args()
    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)
    demo = json.loads(Path(args.demo).read_text())
    resultado = grabar(demo, args.web.rstrip("/"), salida)
    try:
        resultado["trazabilidad"] = trazabilidad(demo)
    except httpx.HTTPError as exc:
        resultado["trazabilidad_error"] = type(exc).__name__
    (salida / "resultado.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=2))
    ok = (
        "error" not in resultado
        and resultado["resumen"] is not None
        and all("respuesta" in r for r in resultado["preguntas"])
    )
    print("RESULTADO:", "OK" if ok else "CON PROBLEMAS", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
