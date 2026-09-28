from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    supabase_anon_key: str
    supabase_jwt_secret: str

    llm_provider: str = "gemini"
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None
    llm_max_tokens: int = 512

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
