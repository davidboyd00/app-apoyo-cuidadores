"""Escritura idempotente para la carga offline de la app móvil.

Un cuidador sin señal registra en el dispositivo con un `client_id` (uuid).
Cuando vuelve la red, la cola reenvía. Si la respuesta se perdió y reintenta,
el servidor no debe duplicar la entrada: devuelve la fila que ya existe.

La unicidad la garantiza la base (índice único `(author_id, client_id)`); aquí
solo se resuelve la carrera entre dos reintentos casi simultáneos.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from postgrest.exceptions import APIError
from supabase import Client


def _por_cliente(db: Client, tabla: str, author_id: str, client_id: UUID) -> dict[str, Any] | None:
    res = (
        db.table(tabla)
        .select("*")
        .eq("author_id", author_id)
        .eq("client_id", str(client_id))
        .limit(1)
        .execute()
        .data
    )
    return res[0] if res else None


def insertar_o_recuperar(
    db: Client,
    tabla: str,
    row: dict[str, Any],
    author_id: str,
    client_id: UUID | None,
) -> tuple[dict[str, Any], bool]:
    """Inserta `row`. Devuelve `(fila, creada)`.

    Si `client_id` ya existe para este autor, devuelve la fila existente con
    `creada=False` y no inserta nada. Sin `client_id` se comporta como un
    insert normal.
    """
    if client_id is not None:
        existente = _por_cliente(db, tabla, author_id, client_id)
        if existente is not None:
            return existente, False
    try:
        data = db.table(tabla).insert(row).execute().data
    except APIError as exc:
        # 23505: otro reintento insertó el mismo client_id entre el select y el insert.
        if client_id is not None and exc.code == "23505":
            existente = _por_cliente(db, tabla, author_id, client_id)
            if existente is not None:
                return existente, False
        raise
    return data[0], True
