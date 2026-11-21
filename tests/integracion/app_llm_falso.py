"""La API real con un LLM SIMULADO y determinista (pruebas y demo local).

Nunca llama a un proveedor externo. El asistente cita las dos entradas más
recientes del contexto (y además una cita inventada, que la API debe
descartar) y arma la respuesta con su texto, así se lee natural en la demo.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from app import llm

_LINEA_ASISTENTE = re.compile(r"^\[([0-9a-f-]{36})\] [^·]*· (\w+)(?: \[corregida\])?: (.+)$", re.M)
_LINEA_RESUMEN = re.compile(r"^[^·]*· (\w+)(?: \[corregida\])?: (.+)$", re.M)


class LLMFalso:
    async def generar(self, prompt_sistema, contexto, pregunta, esquema=None):
        if esquema is not None:
            entradas = _LINEA_ASISTENTE.findall(contexto)[:2]
            if not entradas:
                return json.dumps(
                    {
                        "respuesta": "No hay registros para responder.",
                        "citas": [],
                        "suficiente": False,
                    }
                )
            texto = " ".join(f"{c.strip().rstrip('.')}." for _, _, c in entradas)
            return json.dumps(
                {
                    "respuesta": f"Según lo registrado: {texto} (respuesta simulada)",
                    "citas": [i for i, _, _ in entradas] + ["00000000-0000-0000-0000-00000000dead"],
                    "suficiente": True,
                }
            )
        tipos = Counter(k for k, _ in _LINEA_RESUMEN.findall(contexto))
        detalle = ", ".join(f"{n} de {k}" for k, n in tipos.most_common())
        return (
            "Resumen simulado (entorno de prueba, sin IA real).\n"
            f"Registros del período: {sum(tipos.values())} ({detalle}).\n"
            "Revisar con la familia los síntomas registrados y la adherencia a medicamentos."
        )


llm._proveedor = LLMFalso

from app.main import app  # noqa: E402,F401
