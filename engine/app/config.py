"""Configuration loaded from environment variables (and an optional local .env file)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Required: the engine refuses to start without a key rather than running unauthenticated.
    eval_engine_api_key: SecretStr = Field(min_length=1)
    database_url: str = "sqlite:///./agentlab_engine.db"

    # How long a SQLite write waits for another writer before giving up.
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=0)

    # Worker tuning. The worker only runs when an executor is configured.
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    # A running run whose owner has not renewed its lease for this long is considered
    # orphaned. Renewed every lease/3 seconds, so it tolerates two missed heartbeats.
    worker_lease_seconds: float = Field(default=30.0, gt=0)
    # On shutdown, how long the in-flight run may keep going before it is interrupted.
    worker_shutdown_grace_seconds: float = Field(default=10.0, ge=0)

    # Which executor runs scenarios. "none": the engine stores and serves runs but refuses
    # new launches (503), because nothing would execute them.
    eval_executor: Literal["none", "langgraph"] = "none"
    # Provider credentials, server-side only. Read from the environment / .env and handed
    # to the client explicitly; never logged or returned.
    openai_api_key: SecretStr | None = None
    # Optional OpenAI-compatible endpoint (default: the real OpenAI API). Lets the engine
    # talk to a local test server or proxy without code changes.
    openai_base_url: str | None = None
    # Budget for ONE model call, including the client's own retries.
    model_timeout_seconds: float = Field(default=60.0, gt=0)
    # Retries the provider client makes for transient errors (429, 5xx, connection).
    model_max_retries: int = Field(default=2, ge=0, le=10)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
