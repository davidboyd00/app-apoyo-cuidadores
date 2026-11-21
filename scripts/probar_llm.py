"""Verifica que el modelo LLM configurado responde (con datos FICTICIOS).

Uso local:   GEMINI_API_KEY=... python scripts/probar_llm.py
Desde el celular: GitHub → Actions → "Probar LLM" → Run workflow.

Prueba las dos llamadas del backend: respuesta del asistente con citas
(JSON estructurado) y texto libre del resumen médico. Imprime modelo, latencia
y si el formato vino bien. Nunca usa datos reales de pacientes.
Sale con código 1 si algo falla (el workflow queda en rojo).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SUPABASE_URL", "https://no-se-usa.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "no-se-usa")

from app import llm  # noqa: E402
from app.config import settings  # noqa: E402

ID_1 = "11111111-1111-1111-1111-111111111111"
ID_2 = "22222222-2222-2222-2222-222222222222"
CONTEXTO_FICTICIO = (
    f"[{ID_1}] 2026-11-20T08:10:00-03:00 · medicamento: Tomó su pastilla de la mañana.\n"
    f"[{ID_2}] 2026-11-20T21:00:00-03:00 · animo: Estuvo tranquila y conversó con la familia."
)


async def main() -> int:
    modelo = settings.gemini_model if settings.llm_provider != "claude" else settings.claude_model
    print(f"Proveedor: {settings.llm_provider} · modelo: {modelo}")
    ok = True

    t0 = time.perf_counter()
    try:
        r = await llm.ask_llm_citado(CONTEXTO_FICTICIO, "¿Tomó su pastilla ayer?", {ID_1, ID_2})
        ms = (time.perf_counter() - t0) * 1000
        print(
            f"Asistente: {ms:.0f} ms · formato JSON válido: {r.formato_valido} · "
            f"citas válidas: {len(r.citas)} · descartadas: {r.descartadas} · suficiente: {r.suficiente}"
        )
        print(f"  Respuesta: {r.respuesta}")
        ok &= r.formato_valido and bool(r.citas)
    except Exception as exc:  # noqa: BLE001
        print(f"Asistente FALLÓ: {type(exc).__name__}: {exc}")
        ok = False

    t0 = time.perf_counter()
    try:
        texto = await llm.ask_llm(CONTEXTO_FICTICIO, "Resume en 2 líneas para el médico.")
        print(f"Resumen: {(time.perf_counter() - t0) * 1000:.0f} ms · {len(texto)} caracteres")
        print(f"  Texto: {texto[:300]}")
    except Exception as exc:  # noqa: BLE001
        print(f"Resumen FALLÓ: {type(exc).__name__}: {exc}")
        ok = False

    print("RESULTADO:", "OK" if ok else "CON PROBLEMAS (revisa GEMINI_MODEL / la clave)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
