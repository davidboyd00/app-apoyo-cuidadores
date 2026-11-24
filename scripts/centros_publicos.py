"""Centros de salud y farmacias desde fuentes públicas (scraper).

Solo datos públicos de establecimientos (nombre, tipo, dirección, teléfono):
ningún dato personal. Fuentes:

- Centros de salud: "Establecimientos de Salud" del DEIS (MINSAL), publicado
  en datos.gob.cl. Se guardan solo los vigentes y de los tipos útiles para
  quien cuida (CESFAM, urgencias, hospitales, postas, demencia, etc.).
- Farmacias: OpenStreetMap (Overpass, © contribuyentes de OpenStreetMap).
  La región y la comuna salen del establecimiento del DEIS más cercano cuando
  la farmacia no las trae. (El registro Farmanet del MINSAL rechaza las
  descargas desde servidores; se intenta igual por si se abre.)

Pasos (los corre el workflow "Centros y farmacias" en GitHub Actions):

    python scripts/centros_publicos.py descargar --crudos crudos
    python scripts/centros_publicos.py normalizar --crudos crudos --salida salida
    python scripts/centros_publicos.py cargar --salida salida   # requiere service role

`normalizar` deja `salida/centros.json` y `salida/centros.sql`. Si los secrets
de Supabase no están en GitHub, `centros.sql` se pega en el SQL Editor.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx

# Nombre que muestra la app por código de región del DEIS, de norte a sur. La
# web usa estos mismos nombres en el filtro de región.
REGIONES = {
    "15": "Arica y Parinacota",
    "01": "Tarapacá",
    "02": "Antofagasta",
    "03": "Atacama",
    "04": "Coquimbo",
    "05": "Valparaíso",
    "13": "Metropolitana",
    "06": "O'Higgins",
    "07": "Maule",
    "16": "Ñuble",
    "08": "Biobío",
    "09": "La Araucanía",
    "14": "Los Ríos",
    "10": "Los Lagos",
    "11": "Aysén",
    "12": "Magallanes",
}

# Tipo del DEIS → tipo que muestra la app. Los que no están (laboratorios,
# dental, vacunatorios, toma de muestras, etc.) no se cargan.
TIPOS_DEIS = {
    "Centro de Salud Familiar (CESFAM)": "CESFAM",
    "Centro Comunitario de Salud Familiar (CECOSF)": "CECOSF",
    "Consultorio General Urbano (CGU)": "Consultorio",
    "Consultorio General Rural (CGR)": "Consultorio",
    "Posta de Salud Rural (PSR)": "Posta rural",
    "Servicio de Atención Primaria de Urgencia (SAPU)": "Urgencia (SAPU/SAR)",
    "Servicio de Atención Primaria de Urgencia de Alta Resolutividad (SAR)": "Urgencia (SAPU/SAR)",
    "Servicio de Urgencia Rural (SUR)": "Urgencia rural (SUR)",
    "Hospital": "Hospital",
    "Hospital de Día": "Hospital",
    "Clínica": "Clínica",
    "Centro de Salud Privado": "Centro médico",
    "Centro de Especialidades": "Centro de especialidades",
    "Centro de Especialidades de Atención Primaria (CEAP)": "Centro de especialidades",
    "Centro de Salud Comunitario para Personas con Demencia": "Centro de demencia",
    "Centro Comunitario de Salud Mental  (COSAM)": "Salud mental (COSAM)",
    "Centro Comunitario de Salud Mental (COSAM)": "Salud mental (COSAM)",
    "Centro de Rehabilitación": "Rehabilitación",
}

FARMANET = {
    "farmacias.json": "https://midas.minsal.cl/farmacia_v2/WS/getLocales.php",
    "farmacias_turno.json": "https://midas.minsal.cl/farmacia_v2/WS/getLocalesTurnos.php",
}
CKAN = "https://datos.gob.cl/api/3/action/package_search"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
CONSULTA_FARMACIAS = """[out:json][timeout:300];
area["ISO3166-1"="CL"][admin_level=2]->.cl;
nwr["amenity"="pharmacy"](area.cl);
out center tags;"""

AGENTE = "CuidaApp/1.0 (proyecto universitario PUC; github.com/davidboyd00/app-apoyo-cuidadores)"


# --- Descarga -----------------------------------------------------------------


def _error(exc: Exception) -> dict:
    return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def descargar(crudos: Path) -> dict:
    crudos.mkdir(parents=True, exist_ok=True)
    manifiesto: dict = {}
    with httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": AGENTE}) as c:
        for nombre, url in FARMANET.items():
            try:
                r = c.get(url)
                r.raise_for_status()
                datos = json.loads(r.content.decode("utf-8-sig"))
                (crudos / nombre).write_text(json.dumps(datos, ensure_ascii=False))
                manifiesto[nombre] = {"ok": True, "registros": len(datos)}
            except Exception as exc:  # noqa: BLE001 - se informa y se sigue
                manifiesto[nombre] = _error(exc)

        try:
            manifiesto["deis.csv"] = _descargar_deis(c, crudos / "deis.csv")
        except Exception as exc:  # noqa: BLE001
            manifiesto["deis.csv"] = _error(exc)

        manifiesto["osm_farmacias.json"] = _descargar_osm(c, crudos / "osm_farmacias.json")

    (crudos / "manifiesto.json").write_text(json.dumps(manifiesto, ensure_ascii=False, indent=2))
    return manifiesto


def _descargar_deis(c: httpx.Client, destino: Path) -> dict:
    """El CSV "Establecimientos de Salud" del DEIS, buscado en datos.gob.cl."""
    r = c.get(CKAN, params={"q": "establecimientos de salud deis", "rows": 20})
    r.raise_for_status()
    for pkg in r.json()["result"]["results"]:
        if pkg.get("title", "").strip().lower() != "establecimientos de salud":
            continue
        for res in pkg.get("resources", []):
            if (res.get("format") or "").lower() == "csv" and res.get("url"):
                archivo = c.get(res["url"], timeout=180)
                archivo.raise_for_status()
                destino.write_bytes(archivo.content)
                return {
                    "ok": True,
                    "url": res["url"],
                    "modificado": res.get("last_modified"),
                    "bytes": len(archivo.content),
                }
    return {"ok": False, "error": "No se encontró el CSV de establecimientos en datos.gob.cl"}


def _descargar_osm(c: httpx.Client, destino: Path) -> dict:
    """Farmacias de Chile en OpenStreetMap. Overpass es comunitario y a veces
    está saturado: se prueban espejos, con pausa entre intentos."""
    errores = []
    for intento in range(2):
        for url in OVERPASS:
            try:
                r = c.post(url, data={"data": CONSULTA_FARMACIAS}, timeout=330)
                r.raise_for_status()
                datos = r.json()
                destino.write_text(json.dumps(datos, ensure_ascii=False))
                return {"ok": True, "url": url, "registros": len(datos.get("elements", []))}
            except Exception as exc:  # noqa: BLE001
                errores.append(f"{url}: {type(exc).__name__}")
            time.sleep(10 * (intento + 1))
    return {"ok": False, "errores": errores}


# --- Normalización ------------------------------------------------------------


def sin_tildes(t: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", t) if unicodedata.category(ch) != "Mn")


def _limpio(t: str | None) -> str | None:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t or None


def telefono(t: str | None) -> str | None:
    """Solo números marcables desde Chile; los formatos antiguos se descartan
    para que "Llamar" nunca abra un número incompleto."""
    d = re.sub(r"\D", "", t or "")
    if len(d) == 11 and d.startswith("56"):
        return "+" + d
    if len(d) == 9 and d.startswith("800"):
        return d
    if len(d) == 10 and d[:3] in ("600", "800"):
        return d
    if len(d) == 9:
        return "+56" + d
    return None


def _direccion_deis(f: dict) -> str | None:
    via = (f.get("TipoViaGlosa") or "").strip()
    prefijo = {"Avenida": "Av.", "Pasaje": "Pje.", "Camino": "Camino", "Ruta": "Ruta"}.get(via, "")
    nombre = _limpio(f.get("NombreVia"))
    numero = _limpio(f.get("Numero"))
    if numero and numero.upper() in ("S/N", "SN", "0"):
        numero = "s/n"
    partes = [p for p in (prefijo, nombre, numero) if p]
    return " ".join(partes) if nombre else None


def _leer_deis(ruta: Path) -> list[dict]:
    texto = ruta.read_bytes().decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(texto), delimiter=";"))


def _normalizar_deis(filas: list[dict]) -> tuple[list[dict], list[tuple]]:
    """Centros vigentes de los tipos útiles + puntos (lat, lng, región, comuna)
    de TODOS los establecimientos, para ubicar las farmacias."""
    centros, puntos = [], []
    for f in filas:
        region = REGIONES.get((f.get("RegionCodigo") or "").strip().zfill(2))
        comuna = _limpio(f.get("ComunaGlosa"))
        try:
            lat, lng = float(f["Latitud"]), float(f["Longitud"])
            if region and -57 < lat < -17 and -110 < lng < -66:
                puntos.append((lat, lng, region, comuna))
        except (KeyError, TypeError, ValueError):
            pass
        tipo = TIPOS_DEIS.get((f.get("TipoEstablecimientoGlosa") or "").strip())
        vigente = (f.get("EstadoFuncionamiento") or "").lower().startswith("vigente")
        nombre = _limpio(f.get("EstablecimientoGlosa"))
        if not (tipo and vigente and region and nombre):
            continue
        centros.append(
            {
                "nombre": nombre,
                "tipo": tipo,
                "region": region,
                "comuna": comuna,
                "direccion": _direccion_deis(f),
                "telefono": telefono(f.get("TelefonoMovil_TelefonoFijo")),
            }
        )
    return centros, puntos


class Ubicador:
    """Región y comuna del establecimiento DEIS más cercano (grilla de 0,1°)."""

    def __init__(self, puntos: list[tuple]):
        self.celdas: dict[tuple[int, int], list[tuple]] = {}
        for p in puntos:
            self.celdas.setdefault(self._celda(p[0], p[1]), []).append(p)
        comunas = {p[3] for p in puntos if p[3]}
        self.comunas = {sin_tildes(c).lower(): c for c in comunas}

    @staticmethod
    def _celda(lat: float, lng: float) -> tuple[int, int]:
        return (math.floor(lat * 10), math.floor(lng * 10))

    def cercano(self, lat: float, lng: float) -> tuple[tuple, float] | None:
        cy, cx = self._celda(lat, lng)
        for radio in (1, 3, 8):
            candidatos = [
                p
                for dy in range(-radio, radio + 1)
                for dx in range(-radio, radio + 1)
                for p in self.celdas.get((cy + dy, cx + dx), [])
            ]
            if candidatos:
                k = math.cos(math.radians(lat))
                distancias = [
                    (math.hypot(p[0] - lat, (p[1] - lng) * k) * 111.0, p) for p in candidatos
                ]
                km, mejor = min(distancias, key=lambda d: d[0])
                return mejor, km
        return None

    def comuna_canonica(self, texto: str | None) -> str | None:
        return self.comunas.get(sin_tildes(texto or "").lower().strip()) if texto else None


def _normalizar_osm(datos: dict, ubicador: Ubicador) -> list[dict]:
    farmacias, vistas = [], set()
    for e in datos.get("elements", []):
        tags = e.get("tags", {})
        nombre = _limpio(tags.get("name") or tags.get("brand"))
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lng = e.get("lon") or (e.get("center") or {}).get("lon")
        if not nombre or lat is None or lng is None:
            continue
        hallado = ubicador.cercano(lat, lng)
        if not hallado or hallado[1] > 40:  # sin establecimiento a menos de 40 km
            continue
        (_, _, region, comuna_cercana), km = hallado
        comuna = ubicador.comuna_canonica(tags.get("addr:city")) or (
            comuna_cercana if km <= 5 else None
        )
        calle = " ".join(x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x)
        clave = (sin_tildes(nombre).lower(), round(lat, 4), round(lng, 4))
        if clave in vistas:
            continue
        vistas.add(clave)
        farmacias.append(
            {
                "nombre": nombre if "farmacia" in nombre.lower() else f"Farmacia {nombre}",
                "tipo": "Farmacia",
                "region": region,
                "comuna": comuna,
                "direccion": _limpio(calle),
                "telefono": telefono(tags.get("phone") or tags.get("contact:phone")),
            }
        )
    return farmacias


def normalizar(crudos: Path, salida: Path) -> dict:
    salida.mkdir(parents=True, exist_ok=True)
    resumen: dict = {}
    centros: list[dict] = []
    puntos: list[tuple] = []

    if (crudos / "deis.csv").exists():
        deis, puntos = _normalizar_deis(_leer_deis(crudos / "deis.csv"))
        centros += deis
        resumen["deis"] = len(deis)
    if (crudos / "osm_farmacias.json").exists() and puntos:
        datos = json.loads((crudos / "osm_farmacias.json").read_text())
        farmacias = _normalizar_osm(datos, Ubicador(puntos))
        centros += farmacias
        resumen["osm_farmacias"] = len(farmacias)

    centros.sort(key=lambda c: (c["region"], c["tipo"], c["nombre"]))
    (salida / "centros.json").write_text(json.dumps(centros, ensure_ascii=False, indent=0))
    (salida / "centros.sql").write_text(sql_insercion(centros))
    resumen["total"] = len(centros)
    resumen["por_tipo"] = _contar(centros, "tipo")
    resumen["por_region"] = _contar(centros, "region")
    resumen["con_telefono"] = sum(1 for c in centros if c["telefono"])
    (salida / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2))
    return resumen


def _contar(centros: list[dict], campo: str) -> dict:
    out: dict[str, int] = {}
    for c in centros:
        out[c[campo]] = out.get(c[campo], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _lit(v: str | None) -> str:
    return "null" if v is None else "'" + str(v).replace("'", "''") + "'"


COLUMNAS = ("nombre", "tipo", "region", "comuna", "direccion", "telefono")


def sql_insercion(centros: list[dict], por_bloque: int = 500) -> str:
    """Reemplaza el contenido de `centers` en una sola transacción."""
    partes = ["begin;", "delete from centers;"]
    for i in range(0, len(centros), por_bloque):
        valores = ",\n".join(
            "(" + ", ".join(_lit(c[k]) for k in COLUMNAS) + ")" for c in centros[i : i + por_bloque]
        )
        partes.append(f"insert into centers ({', '.join(COLUMNAS)}) values\n{valores};")
    partes.append("commit;")
    return "\n".join(partes) + "\n"


# --- Carga directa (service role, solo en GitHub Actions) ----------------------


def cargar(salida: Path) -> int:
    url, clave = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not clave:
        print("Sin SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY: no se carga (usa centros.sql).")
        return 0
    centros = json.loads((salida / "centros.json").read_text())
    if not centros:
        print("No hay centros normalizados: no se toca la tabla.")
        return 1
    h = {"apikey": clave, "Authorization": f"Bearer {clave}", "Prefer": "return=minimal"}
    with httpx.Client(base_url=f"{url.rstrip('/')}/rest/v1", headers=h, timeout=60) as c:
        c.delete("/centers", params={"id": "not.is.null"}).raise_for_status()
        for i in range(0, len(centros), 500):
            c.post("/centers", json=centros[i : i + 500]).raise_for_status()
    print(f"Cargados {len(centros)} centros.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="paso", required=True)
    d = sub.add_parser("descargar")
    d.add_argument("--crudos", default="crudos")
    n = sub.add_parser("normalizar")
    n.add_argument("--crudos", default="crudos")
    n.add_argument("--salida", default="salida")
    c = sub.add_parser("cargar")
    c.add_argument("--salida", default="salida")
    a = p.parse_args()

    if a.paso == "descargar":
        print(json.dumps(descargar(Path(a.crudos)), ensure_ascii=False, indent=2))
    elif a.paso == "normalizar":
        resumen = normalizar(Path(a.crudos), Path(a.salida))
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
        return 0 if resumen["total"] else 1
    elif a.paso == "cargar":
        return cargar(Path(a.salida))
    return 0


if __name__ == "__main__":
    sys.exit(main())
