"""Configuration loaded from environment variables (and an optional local .env file)."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Required: the engine refuses to start without a key rather than running unauthenticated.
    eval_engine_api_key: SecretStr = Field(min_length=1)
    database_url: str = "sqlite:///./agentlab_engine.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
