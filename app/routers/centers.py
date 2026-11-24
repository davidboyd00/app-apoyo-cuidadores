import re
import unicodedata

from fastapi import APIRouter, Depends, Query
from supabase import Client

from ..deps import get_db
from ..schemas import Center

router = APIRouter(prefix="/centers", tags=["centers"])


def patron_busqueda(texto: str) -> str | None:
    """Patrón `ilike` tolerante a tildes y mayúsculas para PostgREST.

    Cada vocal pasa a `_` (un carácter cualquiera), así "concepcion" encuentra
    "Concepción". Los espacios y signos pasan a `*` (cualquier tramo). Solo
    quedan letras, números, `_` y `*`: nada que rompa el filtro `or` de PostgREST.
    """
    t = unicodedata.normalize("NFD", texto.lower())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn" or ch == "̃")
    t = unicodedata.normalize("NFC", t)  # conserva la ñ si se escribió
    t = re.sub(r"[aeiou]", "_", t)
    t = re.sub(r"[^a-z0-9ñ_]+", "*", t).strip("*")
    return f"*{t}*" if len(t.replace("_", "")) >= 2 else None


@router.get("", response_model=list[Center])
def listar(
    region: str | None = None,
    tipo: str | None = None,
    q: str | None = Query(
        default=None, max_length=80, description="Busca en nombre, comuna y dirección."
    ),
    limit: int = Query(50, ge=1, le=200),
    db: Client = Depends(get_db),
):
    # Tabla pública poblada por el scraper (scripts/centros_publicos.py).
    # RLS permite lectura anónima. Se filtra en el servidor: son miles.
    consulta = db.table("centers").select("*").order("nombre").limit(limit)
    if region:
        consulta = consulta.eq("region", region)
    if tipo:
        consulta = consulta.eq("tipo", tipo)
    patron = patron_busqueda(q) if q else None
    if patron:
        consulta = consulta.or_(
            f"nombre.ilike.{patron},comuna.ilike.{patron},direccion.ilike.{patron}"
        )
    return consulta.execute().data
