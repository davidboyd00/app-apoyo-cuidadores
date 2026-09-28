"""Handlers globales de errores.

Convención de respuesta de error (para el contrato con móvil):

    {
      "detail": "<mensaje corto en español, accionable>",
      "errores": [ {"campo": "...", "msg": "..."}, ... ]   // solo en 422
    }

Códigos:
    400 — datos inválidos / operación no permitida por reglas de negocio
    401 — token ausente o inválido
    403 — autenticado pero sin permisos (mapea SQLSTATE 42501)
    404 — no existe o el usuario no puede verlo (RLS colapsa ambos)
    409 — conflicto (violación de unique / duplicado)
    422 — validación de payload (pydantic)
    500 — error inesperado (log + mensaje genérico al cliente)
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError

log = logging.getLogger("caregivers")


# Traducciones cortas para tipos de error de Pydantic más comunes en este
# proyecto. No agotamos todos: los que no matchean caen al mensaje original
# (que Pydantic ya arma en inglés técnico, aceptable como fallback).
_MSG_PYDANTIC: dict[str, str] = {
    "missing": "Campo requerido",
    "string_type": "Debe ser texto",
    "int_type": "Debe ser un número entero",
    "int_parsing": "Debe ser un número entero",
    "float_type": "Debe ser un número",
    "bool_type": "Debe ser verdadero o falso",
    "datetime_type": "Debe ser una fecha/hora ISO 8601",
    "datetime_parsing": "Fecha/hora inválida (usa ISO 8601)",
    "uuid_type": "Debe ser un UUID válido",
    "uuid_parsing": "UUID inválido",
    "value_error": "Valor inválido",
    "greater_than": "El valor debe ser mayor",
    "string_too_short": "El texto es demasiado corto",
    "list_type": "Debe ser una lista",
    "enum": "Valor no permitido",
    "literal_error": "Valor no permitido",
}


def _campo(loc: tuple) -> str:
    # loc típicamente empieza con "body" / "query" / "path"; lo omitimos.
    return ".".join(str(p) for p in loc[1:]) if len(loc) > 1 else str(loc[0])


# Mapa SQLSTATE → (http_status, mensaje en español).
# Documentado en https://www.postgresql.org/docs/current/errcodes-appendix.html
_SQLSTATE: dict[str, tuple[int, str]] = {
    "42501": (403, "No tienes permisos para realizar esta operación"),
    "23505": (409, "El recurso ya existe (violación de unicidad)"),
    "23503": (400, "Referencia a un recurso inexistente"),
    "23514": (400, "Los datos no cumplen una restricción de la base"),
    "23502": (400, "Falta un campo requerido"),
    # PostgREST reporta filas no encontradas con estos códigos:
    "PGRST116": (404, "Recurso no encontrado"),
}


def register(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errores = [
            {
                "campo": _campo(e["loc"]),
                "msg": _MSG_PYDANTIC.get(e["type"], e.get("msg", "Inválido")),
            }
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": "Datos de la solicitud inválidos", "errores": errores},
        )

    @app.exception_handler(APIError)
    async def _postgrest(request: Request, exc: APIError):
        status, msg = _SQLSTATE.get(exc.code or "", (500, "Error consultando la base de datos"))
        if status >= 500:
            # Solo logueamos server errors — 4xx son "esperados" y hacen ruido.
            log.warning("PostgREST %s en %s: %s", exc.code, request.url.path, exc.message)
        return JSONResponse(status_code=status, content={"detail": msg})
