"""Configuration. Every value has a working default; offline mode needs no keys."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from anvil.domain.enums import RunMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ANVIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- runtime -----------------------------------------------------------
    mode: RunMode = RunMode.OFFLINE
    env: Literal["local", "ci", "demo"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    #: Every stochastic component derives from this. Same seed, same batch.
    seed: int = 20260902

    # --- infrastructure ----------------------------------------------------
    database_url: str = "postgresql+asyncpg://anvil:anvil@localhost:5432/anvil"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_statement_timeout_ms: int = 15_000

    # --- live-mode credentials --------------------------------------------
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")

    # --- models -------------------------------------------------------------
    model_planner: str = "claude-opus-5"
    model_classifier: str = "claude-sonnet-5"
    model_composer: str = "claude-sonnet-5"

    # --- guardrails ---------------------------------------------------------
    webhook_tolerance_seconds: int = 300
    llm_max_retries: int = 3
    llm_timeout_seconds: int = 60
    llm_max_output_tokens: int = 4096

    # --- paths ---------------------------------------------------------------
    fixtures_dir: str = "anvil/llm/fixtures"

    @field_validator("seed")
    @classmethod
    def _seed_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("seed must be positive so runs stay reproducible")
        return v

    @model_validator(mode="after")
    def _live_mode_needs_credentials(self) -> Settings:
        if self.mode is RunMode.LIVE:
            missing = [
                name
                for name, value in (
                    ("RAZORPAY_KEY_ID", self.razorpay_key_id),
                    ("RAZORPAY_KEY_SECRET", self.razorpay_key_secret.get_secret_value()),
                    ("RAZORPAY_WEBHOOK_SECRET", self.razorpay_webhook_secret.get_secret_value()),
                    ("ANTHROPIC_API_KEY", self.anthropic_api_key.get_secret_value()),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "ANVIL_MODE=live requires: "
                    + ", ".join(f"ANVIL_{m}" for m in missing)
                    + ". Unset ANVIL_MODE to run fully offline with no credentials."
                )
        return self

    @property
    def is_offline(self) -> bool:
        return self.mode is RunMode.OFFLINE

    @property
    def sync_database_url(self) -> str:
        """psycopg URL for Alembic and the LangGraph Postgres checkpointer."""
        return (
            self.database_url.replace("+asyncpg", "").replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
            if "+asyncpg" in self.database_url
            else self.database_url
        )

    @property
    def raw_database_url(self) -> str:
        """Driverless URL, for libraries that build their own connection."""
        return self.database_url.replace("+asyncpg", "").replace("+psycopg", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings_field = Field  # re-export so modules need one import for config helpers
