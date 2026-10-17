"""Environment-only application configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Runtime settings. Credentials are never given a source-code default."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ICICI Prudential Facts-Only RAG Assistant"
    app_env: str = "development"
    log_level: str = "INFO"
    # Answer generation (Gemini). The key is only read server-side and never returned by the API.
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.8-flash"
    # Tried in order when the primary model is overloaded (HTTP 503) or rate-limited.
    gemini_fallback_models: str = "gemini-3.7-flash,gemini-3.5-flash"
    llm_timeout_seconds: float = Field(default=45.0, gt=0)
    chat_rate_limit_per_minute: int = Field(default=20, ge=1)
    bge_model: str = "BAAI/bge-small-en-v1.5"
    reranker_model: str = "BAAI/bge-reranker-base"
    # Minimum cross-encoder relevance probability for a chunk to count as evidence.
    rerank_min_score: float = Field(default=0.5, ge=0.0, le=1.0)
    raw_sources_dir: Path = Field(default=Path("data/raw_sources"))
    extracted_text_dir: Path = Field(default=Path("data/extracted_text"))
    processed_dir: Path = Field(default=Path("data/processed"))
    indexes_dir: Path = Field(default=Path("data/indexes"))
    logs_dir: Path = Field(default=Path("data/logs"))
    source_registry_path: Path = Field(default=Path("data/source_registry.json"))
    tesseract_cmd: str = "tesseract"
    ocr_dpi: int = Field(default=300, ge=150, le=600)
    ocr_check_dpi: int = Field(default=400, ge=150, le=600)

    @field_validator("app_env")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        allowed = {"development", "test", "production"}
        value = value.lower().strip()
        if value not in allowed:
            raise ValueError(f"APP_ENV must be one of {sorted(allowed)}")
        return value

    def resolved_path(self, path: Path) -> Path:
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def storage_paths(self) -> dict[str, Path]:
        return {
            "raw_sources": self.resolved_path(self.raw_sources_dir),
            "extracted_text": self.resolved_path(self.extracted_text_dir),
            "processed": self.resolved_path(self.processed_dir),
            "indexes": self.resolved_path(self.indexes_dir),
            "logs": self.resolved_path(self.logs_dir),
        }

    @property
    def llm_configured(self) -> bool:
        return self.gemini_api_key is not None and bool(self.gemini_api_key.get_secret_value().strip())

    def ensure_storage_directories(self) -> None:
        for directory in self.storage_paths.values():
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
