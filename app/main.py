import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import errors
from .config import settings
from .deps import require_consent
from .routers import (
    assistant,
    audit,
    centers,
    entries,
    me,
    medications,
    members,
    metrics,
    patients,
    summaries,
)

log = logging.getLogger("caregivers")

app = FastAPI(
    title="Caregivers Backend",
    description="Backend de la app de apoyo a cuidadores (Grupo 7 · GPTI PUC 2026-2)",
    version="0.1.0",
)

# CORS: la lista de orígenes viene de la env var CORS_ORIGINS (separada por
# coma). Si no está definida, arrancamos con "*" y loguéamos WARNING —
# aceptable en dev, NO en producción.
_cors_origins = settings.cors_origins_list()
if _cors_origins == ["*"]:
    log.warning("CORS_ORIGINS no está seteado: usando '*'. NO desplegar producción así.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

errors.register(app)

# Ley 21.719: los routers de negocio requieren consentimiento vigente.
# Exentos: `me` (derechos ARCO deben poder ejercerse siempre) y `centers`
# (lectura anónima de datos públicos).
_needs_consent = [Depends(require_consent)]

app.include_router(patients.router, dependencies=_needs_consent)
app.include_router(entries.router, dependencies=_needs_consent)
app.include_router(medications.router, dependencies=_needs_consent)
app.include_router(members.router, dependencies=_needs_consent)
app.include_router(assistant.router, dependencies=_needs_consent)
app.include_router(summaries.router, dependencies=_needs_consent)
app.include_router(metrics.router, dependencies=_needs_consent)
app.include_router(audit.router, dependencies=_needs_consent)
app.include_router(centers.router)
app.include_router(me.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
