"""Proxy mínimo que imita las rutas de Supabase sobre un PostgREST local.

- /rest/v1/*  → PostgREST (lo que usa supabase-py en la API).
- /auth/v1/user → usuario del JWT (lo que consulta supabase-js en la web).
Solo para el stack de integración.
"""

from __future__ import annotations

import os

import httpx
from jose import jwt
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

POSTGREST = os.environ["POSTGREST_URL"]
SECRETO = os.environ["JWT_SECRETO"]
_PASAN = {
    "authorization",
    "content-type",
    "accept",
    "prefer",
    "range",
    "accept-profile",
    "content-profile",
}
_VUELVEN = {"content-type", "content-range", "preference-applied", "location"}
_cliente = httpx.AsyncClient(base_url=POSTGREST, timeout=30)


async def rest(request: Request) -> Response:
    ruta = request.path_params["ruta"]
    headers = {k: v for k, v in request.headers.items() if k.lower() in _PASAN}
    r = await _cliente.request(
        request.method,
        f"/{ruta}",
        params=request.query_params,
        headers=headers,
        content=await request.body(),
    )
    vuelven = {k: v for k, v in r.headers.items() if k.lower() in _VUELVEN}
    return Response(r.content, status_code=r.status_code, headers=vuelven)


async def usuario(request: Request) -> Response:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    try:
        claims = jwt.decode(token, SECRETO, algorithms=["HS256"], audience="authenticated")
    except Exception:  # noqa: BLE001
        return JSONResponse({"msg": "invalid JWT"}, status_code=401)
    return JSONResponse(
        {
            "id": claims["sub"],
            "aud": "authenticated",
            "role": "authenticated",
            "email": claims.get("email"),
            "app_metadata": {},
            "user_metadata": {},
            "created_at": "2026-01-01T00:00:00Z",
        }
    )


async def logout(request: Request) -> Response:
    # Como GoTrue: cerrar sesión responde 204 (no hay sesiones de servidor acá).
    return Response(status_code=204)


app = Starlette(
    routes=[
        Route("/rest/v1/{ruta:path}", rest, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
        Route("/auth/v1/user", usuario, methods=["GET"]),
        Route("/auth/v1/logout", logout, methods=["POST"]),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["content-range"],
        )
    ],
)
