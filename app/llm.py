"""Capa LLM con proveedor abstraído.

El LLM nunca toca la base (regla sagrada #3): recibe el contexto ya armado por
la API (bitácora autorizada por RLS) y responde SOLO con eso.

- Determinismo (EDT 3.1.2): temperatura 0 por defecto y reglas explícitas en
  el prompt (no inventar, decir cuando no alcanza, no indicar tratamientos).
- Salida estructurada (EDT 3.1.1): el asistente responde un JSON con
  `respuesta`, `citas` y `suficiente`. Las citas se filtran contra los ids que
  la API puso en el contexto: un id inventado por el modelo nunca llega a la base.

Los errores (`LLMError`) nunca llevan contenido clínico: solo el motivo técnico.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from .config import settings

log = logging.getLogger("llm")

PROMPT_SISTEMA = (
    "Eres un asistente para familiares que cuidan a una persona dependiente. "
    "Usa ÚNICAMENTE la información del CONTEXTO, que son registros de su bitácora de cuidado. "
    "Reglas: "
    "1) No inventes datos, fechas, dosis, diagnósticos ni causas. "
    "2) Si el contexto no alcanza para responder, dilo con claridad. "
    "3) No indiques tratamientos ni cambios de medicación; ante señales de alarma, "
    "sugiere consultar al equipo de salud. "
    "4) Responde en español, con lenguaje simple."
)

INSTRUCCION_CITAS = (
    "Cada línea del contexto empieza con el id de la entrada entre corchetes. "
    "Responde breve (máximo 5 líneas). Devuelve un JSON con: "
    "`respuesta` (el texto para la familia), "
    "`citas` (los ids exactos, sin corchetes, de las entradas en que te basaste; "
    "lista vacía si no usaste ninguna) y "
    "`suficiente` (false si el contexto no alcanza para responder la pregunta)."
)

ESQUEMA_RESPUESTA: dict = {
    "type": "object",
    "properties": {
        "respuesta": {"type": "string"},
        "citas": {"type": "array", "items": {"type": "string"}},
        "suficiente": {"type": "boolean"},
    },
    "required": ["respuesta", "citas", "suficiente"],
}


class LLMError(RuntimeError):
    """El proveedor no entregó una respuesta utilizable (sin contenido clínico)."""


class LLMProvider(Protocol):
    async def generar(
        self,
        prompt_sistema: str,
        contexto: str,
        pregunta: str,
        esquema: dict | None = None,
        max_tokens: int | None = None,
    ) -> str: ...


def _mensaje(contexto: str, pregunta: str) -> str:
    return f"CONTEXTO:\n{contexto}\n\nPREGUNTA:\n{pregunta}"


def _motivo_fin(resp) -> str:
    """Motivo técnico de una respuesta vacía (tokens, seguridad...), sin contenido."""
    try:
        bloqueo = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
        if bloqueo:
            return f"bloqueo {getattr(bloqueo, 'name', bloqueo)}"
        candidato = (resp.candidates or [None])[0]
        razon = getattr(candidato, "finish_reason", None)
        if razon:
            return str(getattr(razon, "name", razon))
    except Exception:  # noqa: BLE001 - solo diagnóstico
        pass
    return "desconocido"


def _cortada_por_limite(resp) -> bool:
    """True si Gemini dejó de escribir por el límite de tokens: el texto está
    incompleto y no se puede mostrar como si estuviera completo."""
    try:
        razon = getattr((resp.candidates or [None])[0], "finish_reason", None)
        return str(getattr(razon, "name", razon)) == "MAX_TOKENS"
    except Exception:  # noqa: BLE001 - sin candidatos: no hay evidencia de corte
        return False


class GeminiProvider:
    async def generar(
        self,
        prompt_sistema: str,
        contexto: str,
        pregunta: str,
        esquema: dict | None = None,
        max_tokens: int | None = None,
    ) -> str:
        from google import genai
        from google.genai import types

        if not settings.gemini_api_key:
            raise LLMError("Falta GEMINI_API_KEY")

        opciones: dict = {
            "system_instruction": prompt_sistema,
            "temperature": settings.llm_temperature,
            "max_output_tokens": max_tokens or settings.llm_max_tokens,
        }
        if settings.gemini_thinking_level:
            opciones["thinking_config"] = types.ThinkingConfig(
                thinking_level=settings.gemini_thinking_level
            )
        if esquema is not None:
            opciones["response_mime_type"] = "application/json"
            opciones["response_json_schema"] = esquema

        client = genai.Client(api_key=settings.gemini_api_key)
        resp = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=_mensaje(contexto, pregunta),
            config=types.GenerateContentConfig(**opciones),
        )
        texto = resp.text
        if not texto:
            raise LLMError(f"Gemini no devolvió texto (motivo: {_motivo_fin(resp)})")
        if _cortada_por_limite(resp):
            # Un resumen médico cortado no puede pasar por completo.
            raise LLMError("Gemini cortó la respuesta por el límite de tokens (MAX_TOKENS)")
        return texto


class ClaudeProvider:
    async def generar(
        self,
        prompt_sistema: str,
        contexto: str,
        pregunta: str,
        esquema: dict | None = None,
        max_tokens: int | None = None,
    ) -> str:
        from anthropic import AsyncAnthropic

        if not settings.anthropic_api_key:
            raise LLMError("Falta ANTHROPIC_API_KEY")

        sistema = prompt_sistema
        if esquema is not None:
            sistema += (
                " Responde SOLO con un objeto JSON válido, sin texto antes ni después, "
                f"que cumpla este esquema: {json.dumps(esquema, ensure_ascii=False)}"
            )
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model=settings.claude_model,
            max_tokens=max_tokens or settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            system=sistema,
            messages=[{"role": "user", "content": _mensaje(contexto, pregunta)}],
        )
        texto = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        if not texto:
            raise LLMError(f"Claude no devolvió texto (motivo: {msg.stop_reason})")
        if msg.stop_reason == "max_tokens":
            raise LLMError("Claude cortó la respuesta por el límite de tokens (max_tokens)")
        return texto


def _proveedor() -> LLMProvider:
    if settings.llm_provider == "claude":
        return ClaudeProvider()
    return GeminiProvider()


# --- Respuesta estructurada del asistente ---


@dataclass
class RespuestaAsistente:
    respuesta: str
    citas: list[str] = field(default_factory=list)
    suficiente: bool = True
    # Ids citados que no estaban en el contexto (el modelo los inventó o los
    # deformó). Se descartan y solo se cuentan, para medir la calidad.
    descartadas: int = 0
    # False si el modelo no devolvió JSON y se usó su texto tal cual.
    formato_valido: bool = True


def _extraer_json(texto: str) -> dict | None:
    candidatos = [texto.strip()]
    bloque = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", texto, re.DOTALL)
    if bloque:
        candidatos.append(bloque.group(1))
    ini, fin = texto.find("{"), texto.rfind("}")
    if 0 <= ini < fin:
        candidatos.append(texto[ini : fin + 1])
    for c in candidatos:
        try:
            datos = json.loads(c)
        except ValueError:
            continue
        if isinstance(datos, dict):
            return datos
    return None


def interpretar(texto: str, ids_validos: set[str]) -> RespuestaAsistente:
    """Convierte la salida del modelo en una respuesta validada.

    - Las citas se normalizan (sin corchetes, minúsculas), se deduplican y se
      filtran contra `ids_validos` (los ids que la API puso en el contexto).
    - Si el modelo no devolvió JSON, se usa su texto como respuesta sin citas.
    """
    datos = _extraer_json(texto)
    if datos is None:
        respuesta = texto.strip()
        if not respuesta:
            raise LLMError("Respuesta vacía del modelo")
        return RespuestaAsistente(respuesta=respuesta, formato_valido=False)

    respuesta = str(datos.get("respuesta") or "").strip()
    if not respuesta:
        raise LLMError("El JSON del modelo no trae `respuesta`")

    crudas = datos.get("citas")
    if not isinstance(crudas, list):
        crudas = []
    validos = {i.lower() for i in ids_validos}
    citas: list[str] = []
    descartadas = 0
    for c in crudas:
        cid = str(c).strip().strip("[]").strip().lower()
        if cid in citas:
            continue
        if cid in validos:
            citas.append(cid)
        else:
            descartadas += 1

    suficiente = datos.get("suficiente")
    return RespuestaAsistente(
        respuesta=respuesta,
        citas=citas,
        suficiente=suficiente if isinstance(suficiente, bool) else True,
        descartadas=descartadas,
    )


async def ask_llm(contexto: str, pregunta: str, max_tokens: int | None = None) -> str:
    """Texto libre (lo usa el resumen médico). Falla con LLMError si la
    respuesta quedó cortada por el límite de tokens."""
    return await _proveedor().generar(PROMPT_SISTEMA, contexto, pregunta, max_tokens=max_tokens)


async def ask_llm_citado(contexto: str, pregunta: str, ids_validos: set[str]) -> RespuestaAsistente:
    """Respuesta del asistente con citas validadas contra el contexto."""
    texto = await _proveedor().generar(
        f"{PROMPT_SISTEMA} {INSTRUCCION_CITAS}", contexto, pregunta, esquema=ESQUEMA_RESPUESTA
    )
    return interpretar(texto, ids_validos)
