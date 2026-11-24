"""El LLM recibe la hora de Chile, no la UTC de la base.

Con UTC, un registro de las 21:15 del 22/11 llegaba como "2026-11-23T00:15" y
el asistente lo informaba como del 23 a las 00:15 (visto en la demo E3).
"""

from app.resumen import _armar_contexto as contexto_resumen
from app.routers.assistant import _armar_contexto as contexto_asistente

ENTRADA = {
    "id": "11111111-1111-1111-1111-111111111111",
    "occurred_at": "2026-11-23T00:15:00+00:00",
    "kind": "medicamento",
    "content": "No quiso tomar el losartán de la noche.",
}


def test_asistente_recibe_hora_de_chile():
    texto = contexto_asistente([ENTRADA])
    assert "22-11-2026 21:15" in texto
    assert "2026-11-23" not in texto and "00:15" not in texto


def test_resumen_recibe_hora_de_chile():
    texto, enviadas = contexto_resumen([ENTRADA], max_entradas=10, max_caracteres=1000)
    assert enviadas == 1
    assert texto.startswith("22-11-2026 21:15 · medicamento")
