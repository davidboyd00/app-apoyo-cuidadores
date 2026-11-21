from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    supabase_anon_key: str
    # Solo para proyectos legacy que firman con HS256. Los proyectos con
    # llaves asimétricas (ES256/RS256) se verifican contra el JWKS público.
    supabase_jwt_secret: str | None = None

    llm_provider: str = "gemini"
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None
    # Modelos configurables por entorno: Google retira modelos con pocos meses
    # de aviso (gemini-1.5-flash ya no existe). Cambiar acá no requiere deploy
    # de código, solo la variable GEMINI_MODEL en Render.
    gemini_model: str = "gemini-3.6-flash"
    # Gemini 3 razona antes de responder y esos tokens cuentan en el límite de
    # salida. "low" basta para leer una bitácora. Vacío = default del modelo.
    gemini_thinking_level: str | None = "low"
    claude_model: str = "claude-haiku-4-5-20251001"
    # Temperatura 0: misma pregunta y mismo contexto → misma respuesta (EDT 3.1.2).
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024
    # Acotar el contexto del LLM: menos tokens de entrada = respuesta más rápida
    # y más barata. Se envían las entradas más recientes hasta estos límites.
    resumen_max_entradas: int = 200
    resumen_max_caracteres: int = 12000

    # Ley 21.719 · versión vigente de la política de privacidad. Cuando cambie,
    # todos los usuarios deben re-aceptar; los endpoints sensibles retornan 409
    # hasta que lo hagan.
    policy_version: str = "2026-09-11"

    # CORS: lista separada por coma de orígenes permitidos. Ej.:
    # "https://caregivers.app,https://staging.caregivers.app".
    # Si no se define, el server arranca con "*" (útil en dev) y loguea
    # un WARNING — nunca desplegar producción sin este setting.
    cors_origins: str | None = None

    def cors_origins_list(self) -> list[str]:
        if not self.cors_origins:
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
