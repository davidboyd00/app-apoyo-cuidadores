"""Centros de salud y farmacias desde fuentes públicas (scraper).

Solo datos públicos de establecimientos (nombre, tipo, dirección, teléfono):
ningún dato personal. Fuentes:

- Farmacias y farmacias de turno: registro de locales del MINSAL (Farmanet).
- Centros de salud: establecimientos del DEIS (MINSAL) en datos.gob.cl.
- Respaldo: OpenStreetMap (Overpass), por región, si una fuente oficial falla.

Pasos (los corre el workflow "Centros y farmacias" en GitHub Actions):

    python scripts/centros_publicos.py descargar --crudos crudos
    python scripts/centros_publicos.py normalizar --crudos crudos --salida salida
    python scripts/centros_publicos.py cargar --salida salida   # requiere service role

`normalizar` deja `salida/centros.json` y `salida/centros.sql`. Si los secrets
de Supabase no están en GitHub, `centros.sql` se pega en el SQL Editor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx

# (código ISO 3166-2, nombre que muestra la app), de norte a sur. La web usa
# estos mismos nombres en el filtro de región.
REGIONES = [
    ("CL-AP", "Arica y Parinacota"),
    ("CL-TA", "Tarapacá"),
    ("CL-AN", "Antofagasta"),
    ("CL-AT", "Atacama"),
    ("CL-CO", "Coquimbo"),
    ("CL-VS", "Valparaíso"),
    ("CL-RM", "Metropolitana"),
    ("CL-LI", "O'Higgins"),
    ("CL-ML", "Maule"),
    ("CL-NB", "Ñuble"),
    ("CL-BI", "Biobío"),
    ("CL-AR", "La Araucanía"),
    ("CL-LR", "Los Ríos"),
    ("CL-LL", "Los Lagos"),
    ("CL-AI", "Aysén"),
    ("CL-MA", "Magallanes"),
]

FARMANET = {
    "farmacias.json": "https://midas.minsal.cl/farmacia_v2/WS/getLocales.php",
    "farmacias_turno.json": "https://midas.minsal.cl/farmacia_v2/WS/getLocalesTurnos.php",
}
CKAN = "https://datos.gob.cl/api/3/action/package_search"
OVERPASS = "https://overpass-api.de/api/interpreter"
CONSULTA_OSM = """[out:json][timeout:180];
area["ISO3166-2"="{codigo}"]->.r;
(
  nwr["amenity"="pharmacy"](area.r);
  nwr["amenity"~"^(hospital|clinic)$"](area.r);
  nwr["healthcare"~"^(centre|hospital|clinic)$"](area.r);
);
out center tags;"""

AGENTE = "CuidaApp/1.0 (proyecto universitario PUC; github.com/davidboyd00/app-apoyo-cuidadores)"


# --- Descarga -----------------------------------------------------------------


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
                manifiesto[nombre] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }

        manifiesto["deis"] = _descargar_deis(c, crudos)

        for codigo, region in REGIONES:
            nombre = f"osm_{codigo}.json"
            try:
                r = c.post(OVERPASS, data={"data": CONSULTA_OSM.format(codigo=codigo)}, timeout=240)
                r.raise_for_status()
                datos = r.json()
                (crudos / nombre).write_text(json.dumps(datos, ensure_ascii=False))
                manifiesto[nombre] = {
                    "ok": True,
                    "region": region,
                    "registros": len(datos.get("elements", [])),
                }
            except Exception as exc:  # noqa: BLE001
                manifiesto[nombre] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            time.sleep(2)  # Overpass es un servicio comunitario: sin ráfagas

    (crudos / "manifiesto.json").write_text(json.dumps(manifiesto, ensure_ascii=False, indent=2))
    return manifiesto


def _descargar_deis(c: httpx.Client, crudos: Path) -> dict:
    """Busca en datos.gob.cl el listado de establecimientos del DEIS y baja
    hasta 3 archivos candidatos (csv/xlsx) para revisarlos al normalizar."""
    try:
        recursos = []
        for q in ("establecimientos de salud", "establecimientos deis"):
            r = c.get(CKAN, params={"q": q, "rows": 20})
            r.raise_for_status()
            for pkg in r.json()["result"]["results"]:
                for res in pkg.get("resources", []):
                    recursos.append(
                        {
                            "dataset": pkg.get("title", ""),
                            "organizacion": (pkg.get("organization") or {}).get("title", ""),
                            "nombre": res.get("name") or "",
                            "formato": (res.get("format") or "").lower(),
                            "url": res.get("url") or "",
                            "modificado": res.get("last_modified") or res.get("created") or "",
                        }
                    )
        (crudos / "deis_recursos.json").write_text(
            json.dumps(recursos, ensure_ascii=False, indent=1)
        )
        candidatos = [
            x
            for x in recursos
            if "stablecimiento" in (x["dataset"] + x["nombre"]).lower()
            and x["formato"] in ("csv", "xlsx", "xls")
            and x["url"]
        ]
        candidatos.sort(key=lambda x: x["modificado"], reverse=True)
        bajados = []
        for i, x in enumerate(candidatos[:3]):
            try:
                r = c.get(x["url"], timeout=180)
                r.raise_for_status()
                if len(r.content) > 40_000_000:
                    continue
                destino = crudos / f"deis_{i}.{x['formato']}"
                destino.write_bytes(r.content)
                bajados.append({"archivo": destino.name, **x, "bytes": len(r.content)})
            except Exception as exc:  # noqa: BLE001
                bajados.append({"error": f"{type(exc).__name__}: {str(exc)[:200]}", **x})
        return {
            "ok": any("archivo" in b for b in bajados),
            "candidatos": len(candidatos),
            "bajados": bajados,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


# --- Normalización ------------------------------------------------------------

MINUSCULAS = {"de", "del", "la", "las", "los", "y", "el", "en"}


def sin_tildes(t: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", t) if unicodedata.category(ch) != "Mn")


def titulo(t: str | None) -> str | None:
    """'FARMACIA CRUZ VERDE DE MAIPU' → 'Farmacia Cruz Verde de Maipu'."""
    if not t:
        return None
    t = re.sub(r"\s+", " ", str(t)).strip()
    if not t:
        return None
    if t != t.upper():  # ya viene con mayúsculas y minúsculas: se respeta
        return t
    palabras = []
    for i, p in enumerate(t.lower().split(" ")):
        palabras.append(p if (i > 0 and p in MINUSCULAS) else p[:1].upper() + p[1:])
    return " ".join(palabras)


def telefono(t: str | None) -> str | None:
    if not t:
        return None
    digitos = re.sub(r"[^\d+]", "", str(t))
    return digitos if len(re.sub(r"\D", "", digitos)) >= 8 else None


def _clave(c: dict) -> tuple:
    return (sin_tildes(c["nombre"]).lower(), sin_tildes(c.get("direccion") or "").lower())


def normalizar(crudos: Path, salida: Path, mapa_farmanet: dict[str, str]) -> dict:
    salida.mkdir(parents=True, exist_ok=True)
    centros: list[dict] = []
    resumen: dict = {}

    # Farmanet: región por código propio de la fuente (mapa revisado a mano).
    for archivo, tipo in (
        ("farmacias.json", "Farmacia"),
        ("farmacias_turno.json", "Farmacia de turno"),
    ):
        ruta = crudos / archivo
        if not ruta.exists():
            continue
        filas = json.loads(ruta.read_text())
        sin_region = 0
        for f in filas:
            region = mapa_farmanet.get(str(f.get("fk_region", "")).strip())
            nombre = titulo(f.get("local_nombre"))
            if not region or not nombre:
                sin_region += 1
                continue
            centros.append(
                {
                    "nombre": nombre,
                    "tipo": tipo,
                    "region": region,
                    "comuna": titulo(f.get("comuna_nombre")),
                    "direccion": titulo(f.get("local_direccion")),
                    "telefono": telefono(f.get("local_telefono")),
                }
            )
        resumen[archivo] = {"filas": len(filas), "descartadas": sin_region}

    # Centros de salud: OSM por región (la región viene de la consulta).
    for codigo, region in REGIONES:
        ruta = crudos / f"osm_{codigo}.json"
        if not ruta.exists():
            continue
        n = 0
        for e in json.loads(ruta.read_text()).get("elements", []):
            tags = e.get("tags", {})
            tipo = _tipo_osm(tags)
            nombre = tags.get("name")
            if not tipo or not nombre or tipo == "Farmacia":
                continue  # las farmacias salen de la fuente oficial
            calle = " ".join(
                x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x
            )
            centros.append(
                {
                    "nombre": nombre.strip(),
                    "tipo": tipo,
                    "region": region,
                    "comuna": tags.get("addr:city") or tags.get("is_in:municipality"),
                    "direccion": calle or None,
                    "telefono": telefono(tags.get("phone") or tags.get("contact:phone")),
                }
            )
            n += 1
        resumen[f"osm_{codigo}"] = n

    # Sin duplicados (misma farmacia en el listado general y en el de turno:
    # queda la de turno, que es la que sirve hoy).
    unicos: dict[tuple, dict] = {}
    for c in centros:
        k = _clave(c) + (c["region"],)
        if k not in unicos or c["tipo"] == "Farmacia de turno":
            unicos[k] = c
    centros = sorted(unicos.values(), key=lambda c: (c["region"], c["tipo"], c["nombre"]))

    (salida / "centros.json").write_text(json.dumps(centros, ensure_ascii=False, indent=0))
    (salida / "centros.sql").write_text(sql_insercion(centros))
    resumen["total"] = len(centros)
    resumen["por_tipo"] = _contar(centros, "tipo")
    resumen["por_region"] = _contar(centros, "region")
    (salida / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2))
    return resumen


def _tipo_osm(tags: dict) -> str | None:
    nombre = sin_tildes(tags.get("name", "")).lower()
    amenity, hc = tags.get("amenity"), tags.get("healthcare")
    if amenity == "pharmacy":
        return "Farmacia"
    if re.search(r"\b(sapu|sar)\b", nombre):
        return "Urgencia (SAPU/SAR)"
    if re.search(r"\bcesfam\b|centro de salud familiar", nombre):
        return "CESFAM"
    if re.search(r"\bcecosf\b", nombre):
        return "CECOSF"
    if amenity == "hospital" or hc == "hospital" or nombre.startswith("hospital"):
        return "Hospital"
    if re.search(r"\b(clinica|clinic)\b", nombre) or amenity == "clinic" or hc == "clinic":
        return "Clínica"
    if hc == "centre" or re.search(r"\b(consultorio|cosam|posta)\b", nombre):
        return "Centro de salud"
    return None


def _contar(centros: list[dict], campo: str) -> dict:
    out: dict[str, int] = {}
    for c in centros:
        out[c[campo]] = out.get(c[campo], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _lit(v: str | None) -> str:
    return "null" if v is None else "'" + str(v).replace("'", "''") + "'"


def sql_insercion(centros: list[dict], por_bloque: int = 500) -> str:
    """Reemplaza el contenido de `centers` en una transacción."""
    partes = ["begin;", "delete from centers;"]
    for i in range(0, len(centros), por_bloque):
        valores = ",\n".join(
            "("
            + ", ".join(
                _lit(c[k]) for k in ("nombre", "tipo", "region", "comuna", "direccion", "telefono")
            )
            + ")"
            for c in centros[i : i + por_bloque]
        )
        partes.append(
            f"insert into centers (nombre, tipo, region, comuna, direccion, telefono) values\n{valores};"
        )
    partes.append("commit;")
    return "\n".join(partes) + "\n"


# --- Carga directa (service role, solo en GitHub Actions) ----------------------


def cargar(salida: Path) -> int:
    url, clave = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not clave:
        print("Sin SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY: no se carga (usa centros.sql).")
        return 0
    centros = json.loads((salida / "centros.json").read_text())
    h = {"apikey": clave, "Authorization": f"Bearer {clave}", "Prefer": "return=minimal"}
    with httpx.Client(base_url=f"{url.rstrip('/')}/rest/v1", headers=h, timeout=60) as c:
        c.delete("/centers", params={"id": "not.is.null"}).raise_for_status()
        for i in range(0, len(centros), 500):
            c.post("/centers", json=centros[i : i + 500]).raise_for_status()
    print(f"Cargados {len(centros)} centros.")
    return 0


# Código de región de Farmanet → nombre de la app. Revisado contra las
# comunas de cada código en la descarga (ver `diagnostico`).
MAPA_FARMANET: dict[str, str] = {}


def diagnostico(crudos: Path) -> dict:
    """Comunas de ejemplo por código de región de Farmanet, para revisar el mapa."""
    out: dict[str, list[str]] = {}
    ruta = crudos / "farmacias.json"
    if ruta.exists():
        for f in json.loads(ruta.read_text()):
            cod = str(f.get("fk_region", "")).strip()
            comuna = titulo(f.get("comuna_nombre")) or "?"
            lista = out.setdefault(cod, [])
            if comuna not in lista and len(lista) < 6:
                lista.append(comuna)
    return dict(sorted(out.items(), key=lambda kv: (len(kv[0]), kv[0])))


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
    g = sub.add_parser("diagnostico")
    g.add_argument("--crudos", default="crudos")
    c = sub.add_parser("cargar")
    c.add_argument("--salida", default="salida")
    a = p.parse_args()

    if a.paso == "descargar":
        print(json.dumps(descargar(Path(a.crudos)), ensure_ascii=False, indent=2))
    elif a.paso == "diagnostico":
        print(json.dumps(diagnostico(Path(a.crudos)), ensure_ascii=False, indent=2))
    elif a.paso == "normalizar":
        print(
            json.dumps(
                normalizar(Path(a.crudos), Path(a.salida), MAPA_FARMANET),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif a.paso == "cargar":
        return cargar(Path(a.salida))
    return 0


if __name__ == "__main__":
    sys.exit(main())
