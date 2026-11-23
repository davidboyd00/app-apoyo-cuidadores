"""Capa LLM: salida estructurada, citas validadas y configuración del proveedor.

No llama a ninguna API real: se reemplazan los clientes de Gemini y Claude por
dobles que registran lo que se les envía (EDT 3.1.1 y 3.1.2).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import llm
from app.config import settings

ID_A = "11111111-1111-1111-1111-111111111111"
ID_B = "22222222-2222-2222-2222-222222222222"
VALIDOS = {ID_A, ID_B}


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- interpretar() ---


def test_json_valido_con_citas():
    r = llm.interpretar(
        json.dumps({"respuesta": "Durmió bien.", "citas": [ID_A], "suficiente": True}), VALIDOS
    )
    assert (r.respuesta, r.citas, r.suficiente, r.descartadas, r.formato_valido) == (
        "Durmió bien.",
        [ID_A],
        True,
        0,
        True,
    )


def test_cita_inventada_se_descarta_y_se_cuenta():
    inventado = "99999999-9999-9999-9999-999999999999"
    r = llm.interpretar(
        json.dumps({"respuesta": "x", "citas": [ID_A, inventado], "suficiente": True}), VALIDOS
    )
    assert r.citas == [ID_A]
    assert r.descartadas == 1


def test_citas_se_normalizan_y_deduplican():
    r = llm.interpretar(
        json.dumps(
            {
                "respuesta": "x",
                "citas": [f"[{ID_A.upper()}]", ID_A, f" {ID_B} "],
                "suficiente": True,
            }
        ),
        VALIDOS,
    )
    assert r.citas == [ID_A, ID_B]
    assert r.descartadas == 0


def test_insuficiente_se_respeta():
    r = llm.interpretar(
        json.dumps({"respuesta": "No hay registros de eso.", "citas": [], "suficiente": False}),
        VALIDOS,
    )
    assert r.suficiente is False
    assert r.citas == []


def test_json_dentro_de_bloque_de_codigo():
    texto = (
        'Aquí va:\n```json\n{"respuesta": "ok", "citas": ["' + ID_B + '"], "suficiente": true}\n```'
    )
    r = llm.interpretar(texto, VALIDOS)
    assert (r.respuesta, r.citas, r.formato_valido) == ("ok", [ID_B], True)


def test_texto_sin_json_se_usa_tal_cual_sin_citas():
    r = llm.interpretar("Comió bien toda la semana.", VALIDOS)
    assert (r.respuesta, r.citas, r.formato_valido) == ("Comió bien toda la semana.", [], False)


def test_citas_con_tipo_incorrecto_no_rompen():
    r = llm.interpretar(json.dumps({"respuesta": "x", "citas": ID_A, "suficiente": "si"}), VALIDOS)
    assert r.citas == []
    assert r.suficiente is True


@pytest.mark.parametrize("texto", ["", "   ", json.dumps({"citas": [], "suficiente": True})])
def test_respuesta_vacia_es_error(texto):
    with pytest.raises(llm.LLMError):
        llm.interpretar(texto, VALIDOS)


# --- GeminiProvider ---


class _GeminiFalso:
    """Reemplaza google.genai.Client: guarda los argumentos de la llamada."""

    ultima: dict = {}
    respuesta = SimpleNamespace(text="hola", candidates=[], prompt_feedback=None)

    def __init__(self, api_key=None, **_):
        _GeminiFalso.ultima = {"api_key": api_key}

        async def generate_content(**kwargs):
            _GeminiFalso.ultima.update(kwargs)
            return _GeminiFalso.respuesta

        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


@pytest.fixture
def gemini(monkeypatch):
    from google import genai

    monkeypatch.setattr(genai, "Client", _GeminiFalso)
    monkeypatch.setattr(settings, "gemini_api_key", "clave-test")
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    _GeminiFalso.respuesta = SimpleNamespace(text="hola", candidates=[], prompt_feedback=None)
    return _GeminiFalso


@pytest.mark.anyio
async def test_gemini_usa_modelo_configurable_y_temperatura_cero(gemini, monkeypatch):
    monkeypatch.setattr(settings, "gemini_model", "gemini-modelo-x")
    texto = await llm.ask_llm("ctx", "pregunta")
    assert texto == "hola"
    cfg = gemini.ultima["config"]
    assert gemini.ultima["model"] == "gemini-modelo-x"
    assert cfg.temperature == 0.0
    assert cfg.max_output_tokens == settings.llm_max_tokens
    assert cfg.thinking_config.thinking_level.value == "LOW"
    assert cfg.response_json_schema is None
    assert "CONTEXTO:\nctx" in gemini.ultima["contents"]


@pytest.mark.anyio
async def test_gemini_estructurado_envia_esquema_json(gemini):
    gemini.respuesta = SimpleNamespace(
        text=json.dumps({"respuesta": "ok", "citas": [ID_A], "suficiente": True}),
        candidates=[],
        prompt_feedback=None,
    )
    r = await llm.ask_llm_citado("ctx", "p", VALIDOS)
    cfg = gemini.ultima["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == llm.ESQUEMA_RESPUESTA
    assert "citas" in cfg.system_instruction
    assert r.citas == [ID_A]


@pytest.mark.anyio
async def test_gemini_sin_nivel_de_razonamiento(gemini, monkeypatch):
    monkeypatch.setattr(settings, "gemini_thinking_level", None)
    await llm.ask_llm("ctx", "p")
    assert gemini.ultima["config"].thinking_config is None


@pytest.mark.anyio
async def test_gemini_respuesta_vacia_informa_motivo_sin_contenido(gemini):
    gemini.respuesta = SimpleNamespace(
        text=None,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="MAX_TOKENS"))],
        prompt_feedback=None,
    )
    with pytest.raises(llm.LLMError, match="MAX_TOKENS"):
        await llm.ask_llm("Mareo al levantarse", "p")


@pytest.mark.anyio
async def test_gemini_sin_clave_falla_claro(gemini, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", None)
    with pytest.raises(llm.LLMError, match="GEMINI_API_KEY"):
        await llm.ask_llm("ctx", "p")


@pytest.mark.anyio
async def test_gemini_texto_cortado_por_limite_es_error(gemini):
    """Con texto parcial y MAX_TOKENS, no se devuelve el pedazo como si fuera todo."""
    gemini.respuesta = SimpleNamespace(
        text="Síntomas y signos: Mareo leve el 18/",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="MAX_TOKENS"))],
        prompt_feedback=None,
    )
    with pytest.raises(llm.LLMError, match="límite de tokens"):
        await llm.ask_llm("ctx", "p")


@pytest.mark.anyio
async def test_gemini_respeta_limite_por_llamada(gemini):
    await llm.ask_llm("ctx", "p", max_tokens=4096)
    assert gemini.ultima["config"].max_output_tokens == 4096


@pytest.mark.anyio
async def test_gemini_termino_normal_devuelve_texto(gemini):
    gemini.respuesta = SimpleNamespace(
        text="completo",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        prompt_feedback=None,
    )
    assert await llm.ask_llm("ctx", "p") == "completo"


# --- ClaudeProvider ---


class _ClaudeFalso:
    ultima: dict = {}

    def __init__(self, api_key=None):
        async def create(**kwargs):
            _ClaudeFalso.ultima = kwargs
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text='{"respuesta": "ok", "citas": [], '),
                    SimpleNamespace(type="text", text='"suficiente": false}'),
                ],
                stop_reason="end_turn",
            )

        self.messages = SimpleNamespace(create=create)


@pytest.mark.anyio
async def test_claude_estructurado_pide_json_y_une_bloques(monkeypatch):
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _ClaudeFalso)
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(settings, "anthropic_api_key", "clave-test")
    r = await llm.ask_llm_citado("ctx", "p", VALIDOS)
    assert r.respuesta == "ok"
    assert r.suficiente is False
    assert _ClaudeFalso.ultima["model"] == settings.claude_model
    assert _ClaudeFalso.ultima["temperature"] == 0.0
    assert "JSON" in _ClaudeFalso.ultima["system"]


@pytest.mark.anyio
async def test_claude_texto_cortado_por_limite_es_error(monkeypatch):
    import anthropic

    class _ClaudeCortado:
        ultima: dict = {}

        def __init__(self, api_key=None):
            async def create(**kwargs):
                _ClaudeCortado.ultima = kwargs
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="Síntomas: Mareo leve el 18/")],
                    stop_reason="max_tokens",
                )

            self.messages = SimpleNamespace(create=create)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _ClaudeCortado)
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(settings, "anthropic_api_key", "clave-test")
    with pytest.raises(llm.LLMError, match="límite de tokens"):
        await llm.ask_llm("ctx", "p", max_tokens=4096)
    assert _ClaudeCortado.ultima["max_tokens"] == 4096
