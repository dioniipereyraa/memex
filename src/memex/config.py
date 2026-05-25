"""Memex configuration.

Reads from environment variables and optionally from a `.env` file.
Default values are safe for local development.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Unified project settings.

    Env var names follow `.env.example`. Some use the `MEMEX_` prefix to
    avoid collisions, others (like OLLAMA_HOST) are the dependency's
    standard names and are respected as-is.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Embeddings backend. Defaults to `fastembed` (zero-config, model is
    # downloaded automatically the first time). Switch to `ollama` if
    # you prefer to coordinate the model with your local Ollama instance.
    embed_backend: str = Field(default="fastembed", alias="MEMEX_EMBED_BACKEND")

    # Model name. If `None`, each backend uses its default:
    # - fastembed: `nomic-ai/nomic-embed-text-v1.5-Q` (~130 MB, quantized).
    # - ollama: `nomic-embed-text`.
    # If set, it must be a valid model for the chosen backend.
    embed_model: str | None = Field(default=None, alias="MEMEX_EMBED_MODEL")
    embed_dim: int = Field(default=768, alias="MEMEX_EMBED_DIM")

    # Ollama-specific config (only used if embed_backend == "ollama").
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")

    db_path: Path = Field(default=Path("./data/memex.db"), alias="MEMEX_DB_PATH")
    exports_dir: Path = Field(default=Path("./data/exports"), alias="MEMEX_EXPORTS_DIR")

    chunk_size: int = Field(default=500, alias="MEMEX_CHUNK_SIZE", ge=64, le=4096)
    chunk_overlap: int = Field(default=50, alias="MEMEX_CHUNK_OVERLAP", ge=0, le=512)

    log_level: str = Field(default="INFO", alias="MEMEX_LOG_LEVEL")

    # Auto-summaries with Claude Haiku. Opt-in: OFF by default to avoid
    # API calls during bulk ingest. Enable with MEMEX_SUMMARY_ENABLED=true
    # plus a set ANTHROPIC_API_KEY.
    summary_enabled: bool = Field(default=False, alias="MEMEX_SUMMARY_ENABLED")
    summary_model: str = Field(default="claude-haiku-4-5-20251001", alias="MEMEX_SUMMARY_MODEL")
    summary_max_tokens: int = Field(default=200, alias="MEMEX_SUMMARY_MAX_TOKENS", ge=32, le=2048)
    # API key uses the standard Anthropic name (no MEMEX_ prefix) so the
    # key already exported in the shell can be reused.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")


def get_settings() -> Settings:
    """Settings factory.

    Use in tests to inject overrides without touching the global singleton.
    """
    return Settings()


settings = get_settings()
