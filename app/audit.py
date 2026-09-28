"""Helper del registro de auditoría (Ley 21.719 · Punto 2).

Escribe vía la función SECURITY DEFINER `audit_write` en la BD — así el
`user_id` se toma de `auth.uid()` y no se puede falsificar desde la API.

Regla dura para toda llamada a `registrar()`:
    NUNCA pongas contenido clínico en `metadata` (nombres, notas, síntomas,
    respuesta del LLM, transcripciones). Solo ids, tipos, códigos y flags.
    Si tienes duda, no lo agregues.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from supabase import Client


def registrar(
    db: Client,
    accion: str,
    recurso_tipo: str,
    recurso_id: UUID | str | None = None,
    patient_id: UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persiste una entrada de auditoría. Falla silenciosamente si la RPC
    falla — la auditoría no debe romper la operación del usuario, pero se
    espera que las policies RLS eviten inconsistencias graves.
    """
    try:
        db.rpc(
            "audit_write",
            {
                "p_accion": accion,
                "p_recurso_tipo": recurso_tipo,
                "p_recurso_id": str(recurso_id) if recurso_id else None,
                "p_patient_id": str(patient_id) if patient_id else None,
                "p_metadata": metadata or {},
            },
        ).execute()
    except Exception:
        # Auditar es "mejor esfuerzo". Un fallo aquí NO debe interrumpir la
        # respuesta al usuario; el fallo se refleja en el gap del audit_log,
        # detectable en revisión.
        pass
