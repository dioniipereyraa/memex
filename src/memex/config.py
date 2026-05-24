"""Configuración de Memex.

Lee de variables de entorno y opcionalmente de un archivo `.env`.
Valores por default seguros para desarrollo local.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings unificados del proyecto.

    Los nombres de las env vars siguen los del `.env.example`. Algunas usan
    prefijo `MEMEX_` para evitar colisiones, otras (como OLLAMA_HOST) son
    nombres estándar de la dependencia y se respetan tal cual.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Backend de embeddings. Por default `fastembed` (zero-config, modelo se baja
    # automáticamente la primera vez). Cambiar a `ollama` si preferís coordinar
    # el modelo con tu instancia local de Ollama.
    embed_backend: str = Field(default="fastembed", alias="MEMEX_EMBED_BACKEND")

    # Nombre del modelo. Si `None`, cada backend usa su default:
    # - fastembed: `nomic-ai/nomic-embed-text-v1.5-Q` (~130 MB, cuantizado).
    # - ollama: `nomic-embed-text`.
    # Si lo seteás, tiene que ser un modelo válido para el backend elegido.
    embed_model: str | None = Field(default=None, alias="MEMEX_EMBED_MODEL")
    embed_dim: int = Field(default=768, alias="MEMEX_EMBED_DIM")

    # Config específica de Ollama (solo se usa si embed_backend == "ollama").
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")

    db_path: Path = Field(default=Path("./data/memex.db"), alias="MEMEX_DB_PATH")
    exports_dir: Path = Field(default=Path("./data/exports"), alias="MEMEX_EXPORTS_DIR")

    chunk_size: int = Field(default=500, alias="MEMEX_CHUNK_SIZE", ge=64, le=4096)
    chunk_overlap: int = Field(default=50, alias="MEMEX_CHUNK_OVERLAP", ge=0, le=512)

    log_level: str = Field(default="INFO", alias="MEMEX_LOG_LEVEL")

    # Auto-summaries con Claude Haiku. Opt-in: por default OFF para evitar
    # llamadas a la API durante ingest masivo. Activar con
    # MEMEX_SUMMARY_ENABLED=true + ANTHROPIC_API_KEY seteada.
    summary_enabled: bool = Field(default=False, alias="MEMEX_SUMMARY_ENABLED")
    summary_model: str = Field(default="claude-haiku-4-5-20251001", alias="MEMEX_SUMMARY_MODEL")
    summary_max_tokens: int = Field(default=200, alias="MEMEX_SUMMARY_MAX_TOKENS", ge=32, le=2048)
    # API key se respeta como nombre estándar de Anthropic (no MEMEX_-prefixed)
    # para reutilizar la key que ya tengas en el shell.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")


def get_settings() -> Settings:
    """Factory para Settings.

    Usar en tests para inyectar overrides sin tocar el singleton global.
    """
    return Settings()


settings = get_settings()
