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

    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")
    embed_model: str = Field(default="nomic-embed-text", alias="MEMEX_EMBED_MODEL")
    embed_dim: int = Field(default=768, alias="MEMEX_EMBED_DIM")

    db_path: Path = Field(default=Path("./data/memex.db"), alias="MEMEX_DB_PATH")
    exports_dir: Path = Field(default=Path("./data/exports"), alias="MEMEX_EXPORTS_DIR")

    chunk_size: int = Field(default=500, alias="MEMEX_CHUNK_SIZE", ge=64, le=4096)
    chunk_overlap: int = Field(default=50, alias="MEMEX_CHUNK_OVERLAP", ge=0, le=512)

    log_level: str = Field(default="INFO", alias="MEMEX_LOG_LEVEL")


def get_settings() -> Settings:
    """Factory para Settings.

    Usar en tests para inyectar overrides sin tocar el singleton global.
    """
    return Settings()


settings = get_settings()
