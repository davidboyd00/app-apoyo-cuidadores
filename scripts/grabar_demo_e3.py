"""Graba la demo del video final E3 en la app de PRODUCCIÓN, como una persona
usando Chrome (EDT 5.3.2).

Sigue la "Guía paso a paso para grabar la demo" (pasos 1 a 11 y sus tiempos) con
la familia FICTICIA de Elba Rojas y cuentas ficticias (@cuidadores.test). Ningún
dato ni cuenta real aparece en pantalla.

Contraseñas de un solo uso: se generan en el runner y nunca salen de él. El
runner publica solo los hash bcrypt (rama `grabacion-claves`); alguien del
equipo los activa en Supabase y, al terminar, se desactivan.

Pasos (los corre el workflow "Grabar demo E3"):

    claves     contraseñas (archivo local, enmascaradas) y hashes para publicar
    esperar    espera hasta que las contraseñas funcionen
    preparar   como Carmen: política, medicamento con dosis próxima, un resumen
               y una pregunta ya hechos (la guía los pide "del día antes")
    grabar     la toma: video webm y marcas.json (inicio real de cada paso)
    mp4        MP4 1920x1080 (zoom 125 %: viewport de 1536x864 escalado)

Diferencias con una toma a mano (avisadas al equipo): el runner no tiene
micrófono, así que el reconocimiento de voz del navegador se reemplaza por uno
que "dicta" la frase de la guía (la app la recibe y la marca "Por voz" igual); y
la vista previa de impresión se muestra con la hoja en modo impresión.
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

WEB = os.environ.get("WEB", "https://caregivers-web-wxm3.onrender.com").rstrip("/")
API = os.environ.get("API", "https://caregivers-backend.onrender.com").rstrip("/")
PACIENTE = os.environ.get("PACIENTE", "de3a0000-0000-4000-8000-0000000000e2")
CUENTAS = {
    "camila": os.environ.get("CUENTA_TOMA", "camila.rojas@cuidadores.test"),
    "carmen": "carmen.rojas@cuidadores.test",
}
TZ = ZoneInfo("America/Santiago")
ANCHO, ALTO = 1536, 864  # 1920x1080 con zoom 125 %

PERSONA = os.environ.get("PERSONA", "Elba Rojas")
BORRAR = os.environ.get("BORRAR", "Se entretuvo armando un puzzle")
TURNO_DE = os.environ.get("TURNO_DE", "Carmen")
MIEMBROS = os.environ.get("MIEMBROS", "Camila Rojas,Carmen Rojas,Pedro Rojas,Jorge Rojas").split(
    ","
)
REGION, TIPO, COMUNA = "Metropolitana", "CESFAM", "Providencia"

FRASE_VOZ = "Se le dio la insulina de la mañana, diez unidades, después del desayuno"
PREGUNTA = "¿Tomó todos sus medicamentos?"

# Inicio de cada paso en el clip (segundos), según la guía.
TIEMPOS = [0, 30, 55, 105, 150, 190, 215, 235, 275, 305, 325, 340]


# --- Claves de un solo uso -----------------------------------------------------


def claves(directorio: Path, publicar: Path) -> None:
    import bcrypt

    directorio.mkdir(parents=True, exist_ok=True)
    contrasenas = {k: secrets.token_urlsafe(24) for k in CUENTAS}
    for v in contrasenas.values():
        print(f"::add-mask::{v}")
    archivo = directorio / "secretas.json"
    archivo.write_text(json.dumps(contrasenas))
    archivo.chmod(0o600)
    hashes = {
        k: {
            "email": CUENTAS[k],
            "hash": bcrypt.hashpw(v.encode(), bcrypt.gensalt(rounds=10)).decode(),
        }
        for k, v in contrasenas.items()
    }
    publicar.parent.mkdir(parents=True, exist_ok=True)
    publicar.write_text(json.dumps(hashes, indent=2))
    print("Hashes listos para activar:", ", ".join(CUENTAS.values()))


def _secretas(directorio: Path) -> dict:
    return json.loads((directorio / "secretas.json").read_text())


# --- Supabase (URL y clave pública salen del bundle de la web) -----------------


@functools.cache
def config_supabase() -> tuple[str, str]:
    html = httpx.get(WEB, timeout=60).text
    js = re.search(r'src="(/assets/[^"]+\.js)"', html)
    if not js:
        raise SystemExit("No encontré el bundle de la web.")
    bundle = httpx.get(WEB + js.group(1), timeout=60).text
    url = re.search(r"https://[a-z0-9]{20}\.supabase\.co", bundle)
    clave = re.search(r"sb_publishable_[A-Za-z0-9_-]+", bundle)
    if not clave:
        for jwt in re.findall(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", bundle):
            cuerpo = jwt.split(".")[1]
            datos = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)))
            if datos.get("role") == "anon":
                clave = re.match(".*", jwt)
                break
    if not url or not clave:
        raise SystemExit("No encontré la URL o la clave pública de Supabase en el bundle.")
    return url.group(0), clave.group(0)


def iniciar_sesion(email: str, contrasena: str) -> str | None:
    url, clave = config_supabase()
    r = httpx.post(
        f"{url}/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": clave},
        json={"email": email, "password": contrasena},
        timeout=30,
    )
    return r.json().get("access_token") if r.status_code == 200 else None


def esperar(directorio: Path, minutos: int = 40) -> None:
    contrasenas = _secretas(directorio)
    limite = time.monotonic() + minutos * 60
    while time.monotonic() < limite:
        listas = {k: bool(iniciar_sesion(CUENTAS[k], v)) for k, v in contrasenas.items()}
        if all(listas.values()):
            print("Contraseñas activas.")
            return
        print("Esperando activación…", listas, flush=True)
        time.sleep(15)
    raise SystemExit("Las contraseñas no se activaron a tiempo.")


# --- Preparación ("del día antes") ---------------------------------------------


def _api(token: str) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=180)


def despertar_api() -> None:
    for _ in range(20):
        try:
            if httpx.get(f"{API}/health", timeout=60).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(5)
    raise SystemExit("La API no despertó.")


def preparar(directorio: Path) -> None:
    despertar_api()
    token = iniciar_sesion(CUENTAS["carmen"], _secretas(directorio)["carmen"])
    if not token:
        raise SystemExit("Carmen no pudo iniciar sesión.")
    with _api(token) as c:
        version = c.get("/me/consent").json()["version_vigente"]
        c.post("/me/consent", json={"version": version}).raise_for_status()

        # Una dosis para marcar "Tomada" en vivo: ~15 min después de ahora.
        ahora = datetime.now(TZ) + timedelta(minutes=15)
        hora = ahora.replace(minute=(ahora.minute // 5) * 5, second=0, microsecond=0)
        meds = c.get(f"/patients/{PACIENTE}/medications").json()
        if not any(m["nombre"] == "Paracetamol" for m in meds):
            c.post(
                f"/patients/{PACIENTE}/medications",
                json={
                    "nombre": "Paracetamol",
                    "dosis": "500 mg",
                    "horarios": [hora.strftime("%H:%M")],
                    "notas": "Si tiene dolor de rodilla",
                },
            ).raise_for_status()

        # Resumen de 30 días ya generado.
        r = c.post(f"/patients/{PACIENTE}/summaries", params={"dias": 30})
        r.raise_for_status()
        for _ in range(60):
            lista = c.get(f"/patients/{PACIENTE}/summaries").json()
            if lista and lista[0]["estado"] == "listo":
                break
            if lista and lista[0]["estado"] == "error":
                c.post(f"/patients/{PACIENTE}/summaries", params={"dias": 30, "forzar": True})
            time.sleep(5)
        else:
            raise SystemExit("El resumen no quedó listo.")

        # Una pregunta ya hecha (respaldo si en vivo tarda).
        for intento in range(3):
            r = c.post(
                f"/patients/{PACIENTE}/assistant/ask",
                json={"pregunta": PREGUNTA, "ventana_dias": 7},
            )
            if r.status_code == 200:
                break
            time.sleep(10 * (intento + 1))
        print(
            "Preparado: política, Paracetamol",
            hora.strftime("%H:%M"),
            "· resumen · pregunta",
            r.status_code,
        )


# --- Grabación -----------------------------------------------------------------

CURSOR_JS = """
(() => {
  const leer = () => { try { return JSON.parse(sessionStorage.getItem('__cursor') || 'null'); } catch { return null; } };
  const poner = (x, y) => {
    let c = document.getElementById('__cursor');
    if (!c) {
      c = document.createElement('div');
      c.id = '__cursor';
      c.innerHTML = '<svg width="28" height="28" viewBox="0 0 28 28"><path d="M4 2 L4 22 L9.5 17 L13 25 L16.4 23.5 L12.8 15.6 L20 15.6 Z" fill="#0f172a" stroke="#ffffff" stroke-width="1.8" stroke-linejoin="round"/></svg>';
      Object.assign(c.style, {position: 'fixed', width: '28px', height: '28px', zIndex: '2147483647',
        pointerEvents: 'none', transform: 'translate(-4px, -2px)', filter: 'drop-shadow(0 1px 2px rgba(0,0,0,.35))'});
      document.documentElement.appendChild(c);
    }
    c.style.left = x + 'px'; c.style.top = y + 'px';
  };
  document.addEventListener('mousemove', (e) => {
    poner(e.clientX, e.clientY);
    try { sessionStorage.setItem('__cursor', JSON.stringify({x: e.clientX, y: e.clientY})); } catch {}
  }, true);
  document.addEventListener('mousedown', (e) => {
    const o = document.createElement('div');
    Object.assign(o.style, {position: 'fixed', left: (e.clientX - 16) + 'px', top: (e.clientY - 16) + 'px',
      width: '32px', height: '32px', borderRadius: '50%', background: 'rgba(11,106,120,.35)',
      zIndex: '2147483646', pointerEvents: 'none', transition: 'transform .45s ease-out, opacity .45s ease-out'});
    document.documentElement.appendChild(o);
    requestAnimationFrame(() => { o.style.transform = 'scale(1.8)'; o.style.opacity = '0'; });
    setTimeout(() => o.remove(), 500);
  }, true);
  const p = leer();
  const iniciar = () => p && poner(p.x, p.y);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar); else iniciar();
})();
"""

# Reemplaza el reconocimiento de voz (el runner no tiene micrófono): "dicta" la
# frase de la guía palabra a palabra, con resultados parciales y uno final, como
# lo entrega Chrome. La app la procesa por su camino normal de dictado.
VOZ_JS = """
(() => {
  const FRASE = %s;
  class Reconocedor {
    constructor() { this.lang = 'es-CL'; this.continuous = false; this.interimResults = false;
      this.onresult = null; this.onerror = null; this.onend = null; this._t = []; this._activo = false; }
    start() {
      this._activo = true;
      const palabras = FRASE.split(' ');
      let i = 0;
      const paso = () => {
        if (!this._activo) return;
        i += 1;
        const final = i === palabras.length;
        const r = [{ transcript: palabras.slice(0, i).join(' ') }];
        r.isFinal = final;
        this.onresult && this.onresult({ resultIndex: 0, results: [r] });
        if (!final) this._t.push(setTimeout(paso, 300));
      };
      this._t.push(setTimeout(paso, 800));
    }
    stop() { this._activo = false; this._t.forEach(clearTimeout); setTimeout(() => this.onend && this.onend(), 150); }
    abort() { this.stop(); }
  }
  window.SpeechRecognition = Reconocedor;
  window.webkitSpeechRecognition = Reconocedor;
})();
""".replace("%s", json.dumps(FRASE_VOZ))

# window.print() en un navegador sin pantalla no abre diálogo: se deja registro
# y la vista de impresión se muestra emulando el medio "print".
PRINT_JS = "window.print = () => { window.__impresiones = (window.__impresiones || 0) + 1; };"

# Menú desplegable dibujado sobre el <select> (los menús nativos no salen en el
# video de un navegador sin pantalla).
MENU_JS = """
(sel) => {
  document.getElementById('__menu')?.remove();
  const r = sel.getBoundingClientRect();
  const m = document.createElement('div');
  m.id = '__menu';
  Object.assign(m.style, {position: 'fixed', left: r.left + 'px', top: (r.bottom + 2) + 'px',
    minWidth: r.width + 'px', background: '#fff', color: '#111', border: '1px solid #c4c4c4',
    borderRadius: '6px', boxShadow: '0 6px 18px rgba(0,0,0,.18)', zIndex: '2147483600',
    font: '16px system-ui, sans-serif', padding: '4px 0'});
  [...sel.options].forEach((o, i) => {
    const it = document.createElement('div');
    it.textContent = o.text; it.dataset.i = i;
    Object.assign(it.style, {padding: '7px 14px', whiteSpace: 'nowrap',
      background: o.selected ? '#e6f2f4' : 'transparent'});
    it.addEventListener('mouseenter', () => { [...m.children].forEach(c => c.style.background = 'transparent'); it.style.background = '#d4e9ed'; });
    m.appendChild(it);
  });
  document.documentElement.appendChild(m);
}
"""


class Toma:
    def __init__(self, page, contexto):
        self.page = page
        self.ctx = contexto
        self.t0: float | None = None
        self.marcas: list[dict] = []
        self.notas: list[str] = []
        self.sin_tiempos = False

    # Movimiento y gestos de una persona: el mouse viaja, se detiene, hace clic.
    def mover(self, loc, pausa: float = 0.6) -> None:
        loc.scroll_into_view_if_needed()
        caja = loc.bounding_box()
        if not caja:
            return
        x = caja["x"] + min(caja["width"] / 2, 60)
        y = caja["y"] + caja["height"] / 2
        self.page.mouse.move(x, y, steps=24)
        self.page.wait_for_timeout(int(pausa * 1000))

    def clic(self, loc, pausa: float = 0.5) -> None:
        self.mover(loc, 0.25)
        loc.click()
        self.page.wait_for_timeout(int(pausa * 1000))

    def escribir(self, loc, texto: str, retardo: int = 55) -> None:
        self.clic(loc, 0.2)
        loc.press_sequentially(texto, delay=retardo)
        self.page.wait_for_timeout(400)

    def elegir(self, sel, etiqueta: str) -> None:
        self.mover(sel, 0.3)
        sel.evaluate(MENU_JS)
        menu = self.page.locator("#__menu")
        opcion = menu.locator("div", has_text=re.compile(f"^{re.escape(etiqueta)}$")).first
        self.page.wait_for_timeout(500)
        self.mover(opcion, 0.5)
        self.page.mouse.down()
        self.page.mouse.up()
        menu.evaluate("m => m.remove()")
        sel.select_option(label=etiqueta)
        self.page.wait_for_timeout(500)

    def pestana(self, nombre: str) -> None:
        nav = self.page.get_by_role("navigation", name="Secciones de la ficha")
        self.clic(nav.get_by_role("link", name=nombre, exact=True), 1.0)

    def paso(self, n: int, titulo: str) -> None:
        """Marca el inicio del paso n y, si el anterior terminó antes, espera su
        tiempo (las voces se graban sobre estos tiempos)."""
        ahora = time.monotonic() - self.t0
        objetivo = TIEMPOS[n - 1]
        if ahora < objetivo and not self.sin_tiempos:
            self.page.wait_for_timeout(int((objetivo - ahora) * 1000))
        real = time.monotonic() - self.t0
        self.marcas.append(
            {
                "paso": n,
                "titulo": titulo,
                "guia": _mmss(objetivo),
                "real": _mmss(real),
                "segundo": round(real, 1),
            }
        )
        print(f"Paso {n} · {titulo} · {_mmss(real)} (guía {_mmss(objetivo)})", flush=True)


def _mmss(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def grabar(directorio: Path, salida: Path) -> int:
    from playwright.sync_api import expect, sync_playwright

    contrasena = _secretas(directorio)["camila"]
    salida.mkdir(parents=True, exist_ok=True)
    despertar_api()
    with sync_playwright() as p:
        try:
            navegador = p.chromium.launch(channel="chrome", slow_mo=40)
            canal = "Google Chrome"
        except Exception:  # noqa: BLE001 - respaldo: el Chromium de Playwright
            navegador = p.chromium.launch(slow_mo=40)
            canal = "Chromium"
        ctx = navegador.new_context(
            viewport={"width": ANCHO, "height": ALTO},
            locale="es-CL",
            timezone_id="America/Santiago",
            record_video_dir=str(salida),
            record_video_size={"width": ANCHO, "height": ALTO},
        )
        ctx.add_init_script(CURSOR_JS)
        ctx.add_init_script(VOZ_JS)
        ctx.add_init_script(PRINT_JS)
        page = ctx.new_page()
        inicio_video = time.monotonic()
        toma = Toma(page, ctx)
        error = None
        try:
            page.goto(WEB + "/")
            expect(page.get_by_label("Correo electrónico")).to_be_visible(timeout=60_000)
            page.mouse.move(ANCHO * 0.62, ALTO * 0.35)
            page.wait_for_timeout(1500)
            toma.t0 = time.monotonic()
            _pasos(toma, page, ctx, contrasena, expect)
        except Exception as exc:  # noqa: BLE001 - el video queda para ver dónde falló
            error = f"{type(exc).__name__}: {str(exc)[:800]}"
            print("ERROR:", error, flush=True)
            page.wait_for_timeout(1500)
        finally:
            video = page.video
            ctx.close()
            navegador.close()
            if video:
                Path(video.path()).rename(salida / "toma.webm")
        datos = {
            "navegador": canal,
            "recorte_inicial_s": round((toma.t0 or inicio_video) - inicio_video, 2),
            "duracion_s": round(time.monotonic() - (toma.t0 or inicio_video), 1),
            "marcas": toma.marcas,
            "notas": toma.notas,
            "error": error,
        }
        (salida / "marcas.json").write_text(json.dumps(datos, ensure_ascii=False, indent=2))
        print(json.dumps(datos, ensure_ascii=False, indent=2))
        return 1 if error else 0


def paso_1(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 1 · Entrar y aceptar la política.
    t.paso(1, "Entrar y aceptar la política")
    t.escribir(page.get_by_label("Correo electrónico"), CUENTAS["camila"], 45)
    t.escribir(page.get_by_label("Contraseña"), contrasena, 30)
    t.clic(page.get_by_role("button", name="Entrar", exact=True), 0.5)
    politica = page.get_by_label("Texto de la política de privacidad")
    expect(politica).to_be_visible(timeout=60_000)
    t.mover(politica, 1.0)
    for _ in range(4):
        page.mouse.wheel(0, 140)
        page.wait_for_timeout(450)
    t.mover(page.get_by_role("button", name="No acepto, salir"), 2.2)
    t.clic(page.get_by_label("He leído y acepto la política de privacidad"), 0.8)
    t.clic(page.get_by_role("button", name="Continuar"), 0.5)
    expect(page.get_by_role("heading", name="Personas que cuidas")).to_be_visible(timeout=60_000)
    page.wait_for_timeout(1200)


def paso_2(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 2 · Ficha de la persona cuidada.
    t.paso(2, "Ficha de la persona cuidada")
    t.clic(page.get_by_role("link", name=re.compile(PERSONA)).first, 1.0)
    notas = page.get_by_role("note", name="Notas importantes")
    expect(notas).to_be_visible(timeout=60_000)
    t.mover(notas, 3.0)
    relevo = page.get_by_text(re.compile(r"autores en los últimos 7 días"))
    expect(relevo).to_be_visible(timeout=30_000)
    t.mover(relevo, 3.5)


def paso_3(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 3 · Bitácora y registro por voz.
    t.paso(3, "Bitácora y registro por voz")
    formulario = page.get_by_role("form", name="Nueva entrada de bitácora")
    expect(formulario).to_be_visible(timeout=30_000)
    t.clic(formulario.locator('label[data-kind="medicamento"]'), 0.8)
    t.clic(formulario.get_by_role("button", name="Dictar"), 0.3)
    expect(formulario.get_by_label("¿Qué pasó?")).to_have_value(
        re.compile("después del desayuno"), timeout=20_000
    )
    page.wait_for_timeout(800)
    t.clic(formulario.get_by_role("button", name="Detener dictado"), 0.6)
    t.mover(formulario.get_by_label("¿Qué pasó?"), 2.0)
    t.clic(formulario.get_by_role("button", name="Registrar"), 0.8)
    nueva = page.locator("article").filter(has_text="diez unidades").first
    expect(nueva).to_be_visible(timeout=30_000)
    expect(nueva.get_by_text("Por voz")).to_be_visible(timeout=15_000)
    t.mover(nueva.get_by_text("Tú", exact=True).first, 1.5)
    t.mover(nueva.locator("time").first, 1.5)
    t.mover(nueva.get_by_text("Por voz"), 2.0)
    filtro = page.get_by_label("Mostrar")
    t.elegir(filtro, "Medicamento")
    page.wait_for_timeout(2500)
    t.elegir(filtro, "Todos los tipos")
    page.wait_for_timeout(1000)


def paso_4(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 4 · SC-02: historial inmutable.
    t.paso(4, "SC-02: historial inmutable")
    nueva = page.locator("article").filter(has_text="diez unidades").first
    t.clic(nueva.get_by_role("button", name=re.compile(r"^Editar")), 0.8)
    edicion = page.get_by_role("form", name="Editar entrada")
    texto = edicion.get_by_label("¿Qué pasó?")
    t.mover(texto, 0.4)
    texto.evaluate(
        "el => { const i = el.value.indexOf('diez'); el.focus(); el.setSelectionRange(i, i + 4); }"
    )
    page.wait_for_timeout(700)
    page.keyboard.type("ocho", delay=110)
    page.wait_for_timeout(500)
    t.escribir(edicion.get_by_label("Motivo del cambio"), "Dosis mal digitada", 60)
    t.clic(edicion.get_by_role("button", name="Guardar cambios"), 1.0)
    editada = page.locator("article").filter(has_text="ocho unidades").first
    expect(editada.get_by_text("Editada")).to_be_visible(timeout=30_000)
    t.mover(editada.get_by_text("Editada"), 1.8)
    t.clic(editada.get_by_role("button", name=re.compile("Ver historial de cambios")), 0.8)
    historial = page.get_by_label("Historial de cambios")
    expect(historial).to_be_visible(timeout=30_000)
    t.mover(historial, 4.0)
    t.clic(editada.get_by_role("button", name=re.compile("Ocultar historial")), 0.8)
    otra = page.locator("article").filter(has_text=BORRAR).first
    t.clic(otra.get_by_role("button", name=re.compile(r"^Borrar entrada de")), 0.6)
    t.escribir(otra.get_by_label("Motivo para borrar"), "Se registró en la persona equivocada", 50)
    t.clic(otra.get_by_role("button", name="Confirmar borrado"), 0.8)
    expect(page.locator("article").filter(has_text=BORRAR)).to_have_count(0, timeout=30_000)
    page.wait_for_timeout(1200)


def paso_5(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 5 · SC-01: registro sin conexión.
    t.paso(5, "SC-01: registro sin conexión")
    ctx.set_offline(True)
    barra = page.get_by_text("Sin conexión.", exact=True)
    expect(barra).to_be_visible(timeout=15_000)
    t.mover(barra, 2.0)
    formulario = page.get_by_role("form", name="Nueva entrada de bitácora")
    t.clic(formulario.locator('label[data-kind="animo"]'), 0.5)
    t.escribir(formulario.get_by_label("¿Qué pasó?"), "Estuvo tranquila en la sala de espera", 50)
    t.clic(formulario.get_by_role("button", name="Registrar"), 0.8)
    aviso = page.get_by_text("Guardado en este dispositivo. Se enviará al volver la conexión.")
    expect(aviso).to_be_visible(timeout=15_000)
    t.mover(aviso, 2.5)
    pendiente = page.get_by_text("Pendiente de sincronizar", exact=True).first
    t.mover(pendiente, 2.5)
    ctx.set_offline(False)
    try:
        expect(page.get_by_text("Pendiente de sincronizar", exact=True)).to_have_count(
            0, timeout=8_000
        )
    except AssertionError:
        t.clic(page.get_by_role("button", name="Sincronizar ahora"), 1.0)
        expect(page.get_by_text("Pendiente de sincronizar", exact=True)).to_have_count(
            0, timeout=30_000
        )
    sincronizada = page.locator("article").filter(has_text="Estuvo tranquila en la sala de espera")
    expect(sincronizada).to_have_count(1, timeout=30_000)
    t.mover(sincronizada.first, 2.5)


def paso_6(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 6 · Turnos.
    t.paso(6, "Turnos")
    t.pestana("Turnos")
    proximos = page.locator("div.stack-sm", has=page.get_by_role("heading", name="Próximos turnos"))
    expect(proximos.locator("li").first).to_be_visible(timeout=30_000)
    t.mover(proximos.locator("li").first, 2.0)
    agendar = page.get_by_role("form", name="Agendar turno").get_by_role(
        "button", name="Agendar turno"
    )
    t.mover(agendar, 1.8)
    recientes = page.locator(
        "div.stack-sm", has=page.get_by_role("heading", name="Turnos recientes (relevo)")
    )
    expect(recientes.locator("li").first).to_be_visible(timeout=30_000)
    turno = recientes.locator("li").filter(has_text=TURNO_DE).first
    if not turno.count():
        turno = recientes.locator("li").first
    t.clic(turno.get_by_role("button", name="Ver lo registrado"), 1.0)
    durante = turno.get_by_text("Registrado durante el turno")
    expect(durante).to_be_visible(timeout=30_000)
    t.mover(durante, 3.5)


def paso_7(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 7 · Medicamentos.
    t.paso(7, "Medicamentos")
    t.pestana("Medicamentos")
    activos = page.get_by_role("heading", name="Medicamentos activos")
    expect(activos).to_be_visible(timeout=30_000)
    t.mover(activos, 1.2)
    proximas = page.get_by_role("heading", name=re.compile("Próximas dosis"))
    t.mover(proximas, 1.0)
    primera = page.get_by_role("group", name=re.compile("^Registrar dosis de")).first
    nombre_dosis = (primera.get_attribute("aria-label") or "").replace("Registrar dosis de ", "")
    t.clic(primera.get_by_role("button", name="Tomada"), 0.3)
    expect(page.get_by_text(re.compile(r"^Tomada · Tú")).first).to_be_visible(timeout=20_000)
    t.mover(page.get_by_text(re.compile(r"^Tomada · Tú")).first, 1.2)
    t.notas.append(f"Dosis marcada en vivo: {nombre_dosis}.")
    siguiente = page.get_by_role("group", name=re.compile("^Registrar dosis de")).first
    t.mover(siguiente.get_by_role("button", name="Más tarde"), 1.8)
    t.mover(page.get_by_role("heading", name="Adherencia · últimos 7 días"), 2.5)


def paso_8(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 8 · Asistente de IA.
    t.paso(8, "Asistente de IA")
    t.pestana("Asistente")
    revisar = page.get_by_label("Revisar")
    expect(revisar).to_be_visible(timeout=30_000)
    t.elegir(revisar, "Últimos 7 días")
    sugerencias = page.get_by_label("Preguntas sugeridas")
    tarjetas = page.locator("article").filter(has_text=PREGUNTA)
    antes = tarjetas.count()
    t.clic(sugerencias.get_by_role("button", name=PREGUNTA), 0.3)
    revisando = page.get_by_text(re.compile("Revisando la bitácora"))
    inicio = time.monotonic()
    try:
        expect(revisando).to_be_visible(timeout=5_000)
        for b in sugerencias.get_by_role("button").all():
            t.mover(b, 0.5)
    except AssertionError:
        pass
    expect(tarjetas).to_have_count(antes + 1, timeout=120_000)
    t.notas.append(f"El asistente respondió en {round(time.monotonic() - inicio, 1)} s.")
    respuesta = tarjetas.first
    t.mover(respuesta, 1.5)
    citas = respuesta.get_by_role("button", name=re.compile(r"^(Basado en|Ocultar registros)"))
    if citas.count():
        if citas.inner_text().startswith("Basado en"):
            t.clic(citas, 1.0)
        t.mover(respuesta, 4.0)
        t.notas.append("La respuesta en vivo cita registros.")
    else:
        t.notas.append("La respuesta en vivo no citó registros.")
    t.notas.append("Respuesta en vivo: " + respuesta.inner_text()[:600].replace("\n", " "))


def paso_9(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 9 · Resumen médico en PDF.
    t.paso(9, "Resumen médico en PDF")
    t.pestana("Resúmenes")
    ver = page.get_by_role("link", name="Ver e imprimir").first
    expect(ver).to_be_visible(timeout=30_000)
    t.clic(ver, 1.0)
    expect(page.get_by_role("heading", name="Resumen de cuidado")).to_be_visible(timeout=30_000)
    traza = page.get_by_label("Trazabilidad del registro")
    t.mover(traza, 1.5)
    t.mover(page.get_by_text(re.compile("evaluación de un profesional de salud")), 2.5)
    t.clic(page.get_by_role("button", name="Imprimir o guardar PDF"), 0.3)
    page.evaluate("window.scrollTo({top: 0})")
    page.emulate_media(media="print")
    page.wait_for_timeout(2000)
    page.mouse.wheel(0, 500)
    page.wait_for_timeout(1500)
    page.emulate_media(media="screen")
    page.evaluate("window.scrollTo({top: 0})")
    page.wait_for_timeout(600)
    t.clic(page.get_by_role("link", name="← Resúmenes"), 0.8)
    t.pestana("Bitácora")


def paso_10(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 10 · Red de cuidado.
    t.paso(10, "Red de cuidado")
    t.pestana("Red de cuidado")
    invitar = page.get_by_role("form", name="Invitar a un familiar")
    expect(invitar).to_be_visible(timeout=30_000)
    for nombre in MIEMBROS:
        fila = page.get_by_text(re.compile(nombre)).first
        if fila.count():
            t.mover(fila, 0.9)
    rol = invitar.get_by_label("Rol")
    t.mover(rol, 0.4)
    rol.evaluate(MENU_JS)
    menu = page.locator("#__menu")
    for etiqueta in ("Administrador/a", "Cuidador/a", "Solo lectura"):
        t.mover(menu.get_by_text(etiqueta, exact=True), 0.9)
    page.wait_for_timeout(600)
    menu.evaluate("m => m.remove()")
    page.mouse.move(ANCHO * 0.75, ALTO * 0.5, steps=15)


def paso_11(t: Toma, page, ctx, contrasena: str, expect) -> None:
    # 11 · Centros y farmacias, y Mi perfil.
    t.paso(11, "Centros y farmacias, y Mi perfil")
    t.clic(page.get_by_role("link", name="Centros", exact=True), 1.0)
    t.elegir(page.get_by_label("Región"), REGION)
    t.elegir(page.get_by_label("Tipo"), TIPO)
    t.escribir(page.get_by_label("Comuna o nombre"), COMUNA, 70)
    primero = page.locator("li").filter(has=page.get_by_role("link", name="Cómo llegar")).first
    expect(primero).to_be_visible(timeout=30_000)
    page.wait_for_timeout(800)
    llamar = primero.get_by_role("link", name="Llamar")
    if llamar.count():
        t.mover(llamar, 1.2)
    t.mover(primero.get_by_role("link", name="Cómo llegar"), 1.2)
    t.clic(page.get_by_role("link", name="Mi perfil", exact=True), 1.0)
    t.mover(page.get_by_role("button", name=re.compile("Descargar mis datos")), 1.5)
    t.mover(page.get_by_role("button", name="Eliminar mi cuenta"), 2.0)
    page.wait_for_timeout(1000)


PASOS = [paso_1, paso_2, paso_3, paso_4, paso_5, paso_6, paso_7, paso_8, paso_9, paso_10, paso_11]


def _pasos(t: Toma, page, ctx, contrasena: str, expect, desde: int = 1) -> None:
    for f in PASOS[desde - 1 :]:
        f(t, page, ctx, contrasena, expect)
    t.marcas.append({"paso": "fin", "real": _mmss(time.monotonic() - t.t0)})


def probar_local(demo: Path, web: str, salida: Path, desde: int) -> int:
    """Prueba los pasos contra la demo local (tests.integracion.demo), con la
    sesión de "ana" inyectada (en local no hay pantalla de Entrar)."""
    from playwright.sync_api import expect, sync_playwright

    datos = json.loads(demo.read_text())
    persona = datos["personas"]["ana"]
    cuerpo = persona["token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)))
    sesion = json.dumps(
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
    salida.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        navegador = p.chromium.launch()
        ctx = navegador.new_context(
            viewport={"width": ANCHO, "height": ALTO},
            locale="es-CL",
            timezone_id="America/Santiago",
            record_video_dir=str(salida),
            record_video_size={"width": ANCHO, "height": ALTO},
        )
        ctx.add_init_script(f"localStorage.setItem('sb-127-auth-token', {json.dumps(sesion)});")
        for js in (CURSOR_JS, VOZ_JS, PRINT_JS):
            ctx.add_init_script(js)
        page = ctx.new_page()
        global WEB
        WEB = web.rstrip("/")
        t = Toma(page, ctx)
        t.sin_tiempos = True
        error = None
        try:
            page.goto(WEB + "/")
            expect(page.get_by_role("heading", name="Personas que cuidas")).to_be_visible(
                timeout=30_000
            )
            if desde > 2:
                page.get_by_role("link", name=re.compile(PERSONA)).first.click()
                expect(page.get_by_role("navigation", name="Secciones de la ficha")).to_be_visible()
            t.t0 = time.monotonic()
            _pasos(t, page, ctx, "", expect, desde=desde)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:1500]}"
            page.screenshot(path=str(salida / "error.png"))
        finally:
            ctx.close()
            navegador.close()
        print(
            json.dumps(
                {"marcas": t.marcas, "notas": t.notas, "error": error}, ensure_ascii=False, indent=2
            )
        )
        return 1 if error else 0


# --- MP4 -----------------------------------------------------------------------


def mp4(salida: Path) -> None:
    import imageio_ffmpeg

    datos = json.loads((salida / "marcas.json").read_text())
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    destino = salida / "3_David_demo.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-ss",
            str(datos["recorte_inicial_s"]),
            "-i",
            str(salida / "toma.webm"),
            "-vf",
            "scale=1920:1080:flags=lanczos,fps=30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(destino),
        ],
        check=True,
    )
    # Cuadros de control: uno al inicio de cada paso (para revisar sin ver todo).
    for m in datos["marcas"]:
        if isinstance(m.get("paso"), int):
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    str(m["segundo"] + 2),
                    "-i",
                    str(destino),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=960:-1",
                    str(salida / f"paso_{m['paso']:02d}.jpg"),
                ],
                check=False,
                capture_output=True,
            )
    print(f"{destino} · {destino.stat().st_size / 1e6:.1f} MB")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="paso", required=True)
    c = sub.add_parser("claves")
    c.add_argument("--claves", default="/tmp/claves")
    c.add_argument("--publicar", default="/tmp/publicar/hashes.json")
    for nombre in ("esperar", "preparar", "grabar"):
        s = sub.add_parser(nombre)
        s.add_argument("--claves", default="/tmp/claves")
        s.add_argument("--salida", default="video")
    pl = sub.add_parser("probar-local")
    pl.add_argument("--demo", required=True)
    pl.add_argument("--web", default="http://127.0.0.1:4173")
    pl.add_argument("--salida", default="/tmp/prueba-demo")
    pl.add_argument("--desde", type=int, default=2)
    m = sub.add_parser("mp4")
    m.add_argument("--salida", default="video")
    a = p.parse_args()

    if a.paso == "claves":
        claves(Path(a.claves), Path(a.publicar))
    elif a.paso == "esperar":
        esperar(Path(a.claves))
    elif a.paso == "preparar":
        preparar(Path(a.claves))
    elif a.paso == "grabar":
        return grabar(Path(a.claves), Path(a.salida))
    elif a.paso == "probar-local":
        return probar_local(Path(a.demo), a.web, Path(a.salida), a.desde)
    elif a.paso == "mp4":
        mp4(Path(a.salida))
    return 0


if __name__ == "__main__":
    sys.exit(main())
