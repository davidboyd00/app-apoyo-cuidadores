"""La API real con un LLM falso y determinista (solo stack de integración).

Cita las dos primeras entradas del contexto, así el test puede comprobar que
las citas viajan, se validan y se guardan. Nunca llama a un proveedor externo.
"""

from __future__ import annotations

import json
import re

from app import llm


class LLMFalso:
    async def generar(self, prompt_sistema, contexto, pregunta, esquema=None):
        ids = re.findall(r"^\[([0-9a-f-]{36})\]", contexto, re.MULTILINE)
        if esquema is not None:
            return json.dumps(
                {
                    "respuesta": f"Según {len(ids)} registros de la bitácora, la semana fue estable.",
                    # Una cita inventada que la API debe descartar.
                    "citas": [*ids[:2], "00000000-0000-0000-0000-00000000dead"],
                    "suficiente": bool(ids),
                }
            )
        return "Semana estable: durmió bien y tomó sus medicamentos."


llm._proveedor = LLMFalso

from app.main import app  # noqa: E402,F401
