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
    mode: RunMode = Field(
        default=RunMode.OFFLINE,
        description="offline needs no credentials at all; live requires all four below.",
    )
    env: Literal["local", "ci", "demo"] = Field(
        default="local", description="Where this process is running."
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", description="Minimum level rendered."
    )
    log_format: Literal["console", "json"] = Field(
        default="console", description="json for aggregation, console for reading."
    )

    #: Every stochastic component derives from this. Same seed, same batch.
    seed: int = Field(
        default=20260902,
        description="Every stochastic component derives from this. Same seed, same batch.",
    )

    # --- infrastructure ----------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://anvil:anvil@localhost:5432/anvil",
        description="Needed only for migrations and integration tests.",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0", description="Reserved; not yet required."
    )
    db_pool_size: int = Field(default=20, description="Connections held open per process.")
    db_max_overflow: int = Field(default=10, description="Extra connections under burst.")
    db_statement_timeout_ms: int = Field(
        default=15_000, description="Server-side cap, so a runaway query cannot hold a worker."
    )

    # --- live-mode credentials --------------------------------------------
    razorpay_key_id: str = Field(default="", description="Test-mode key id, rzp_test_...")
    razorpay_key_secret: SecretStr = Field(
        default=SecretStr(""), description="Shown once by the dashboard."
    )
    razorpay_webhook_secret: SecretStr = Field(
        default=SecretStr(""), description="Signs inbound webhooks; you choose this value."
    )
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""), description="Unset in offline mode; fixtures are used instead."
    )

    # --- models -------------------------------------------------------------
    model_planner: str = Field(
        default="claude-opus-5", description="Planning: judgement under a live budget."
    )
    model_classifier: str = Field(
        default="claude-sonnet-5", description="High-volume classification of unmapped codes."
    )
    model_composer: str = Field(
        default="claude-sonnet-5", description="Customer-facing copy, per language and cause."
    )

    # --- guardrails ---------------------------------------------------------
    webhook_tolerance_seconds: int = Field(
        default=300, description="Replay window. A payload older than this is rejected."
    )
    llm_max_retries: int = Field(
        default=3, description="Retries on malformed structured output, with the error appended."
    )
    llm_timeout_seconds: int = Field(default=60, description="Per model call.")
    llm_max_output_tokens: int = Field(default=4096, description="Ceiling per model call.")

    # --- paths ---------------------------------------------------------------
    fixtures_dir: str = Field(
        default="anvil/llm/fixtures", description="Recorded model responses used in offline mode."
    )

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
