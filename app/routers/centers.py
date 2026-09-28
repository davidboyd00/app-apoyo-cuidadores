from fastapi import APIRouter, Depends, Query
from supabase import Client

from ..deps import get_db
from ..schemas import Center

router = APIRouter(prefix="/centers", tags=["centers"])


@router.get("", response_model=list[Center])
def listar(
    region: str | None = None,
    tipo: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Client = Depends(get_db),
):
    # Tabla pública poblada por scrapers. RLS permite lectura anónima.
    q = db.table("centers").select("*").limit(limit)
    if region:
        q = q.eq("region", region)
    if tipo:
        q = q.eq("tipo", tipo)
    return q.execute().data
