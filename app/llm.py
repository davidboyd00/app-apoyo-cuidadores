"""Capa LLM con proveedor abstraído.

El LLM nunca toca la base: recibe el contexto ya armado por la API
(bitácora autorizada por RLS) y responde SOLO con eso.
"""

from typing import Protocol

from .config import settings

PROMPT_SISTEMA = (
    "Eres un asistente para cuidadores familiares. "
    "Responde ÚNICAMENTE con la información entregada en el contexto. "
    "Si el contexto no alcanza para responder, dilo explícitamente. "
    "No inventes datos clínicos. Responde en español, breve y accionable."
)


class LLMProvider(Protocol):
    async def generar(self, prompt_sistema: str, contexto: str, pregunta: str) -> str: ...


class GeminiProvider:
    async def generar(self, prompt_sistema: str, contexto: str, pregunta: str) -> str:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=prompt_sistema)
        resp = await model.generate_content_async(
            f"CONTEXTO:\n{contexto}\n\nPREGUNTA:\n{pregunta}",
            generation_config={"max_output_tokens": settings.llm_max_tokens},
        )
        return resp.text


class ClaudeProvider:
    async def generar(self, prompt_sistema: str, contexto: str, pregunta: str) -> str:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=settings.llm_max_tokens,
            system=prompt_sistema,
            messages=[
                {"role": "user", "content": f"CONTEXTO:\n{contexto}\n\nPREGUNTA:\n{pregunta}"}
            ],
        )
        return msg.content[0].text


def _proveedor() -> LLMProvider:
    if settings.llm_provider == "claude":
        return ClaudeProvider()
    return GeminiProvider()


async def ask_llm(contexto: str, pregunta: str) -> str:
    return await _proveedor().generar(PROMPT_SISTEMA, contexto, pregunta)
