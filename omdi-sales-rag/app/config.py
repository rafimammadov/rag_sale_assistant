from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "OMDI Sales RAG"
    environment: str = "development"
    public_base_url: str = "http://localhost:8000"
    data_dir: Path = Path("./data")
    database_url: str = "sqlite:///./data/app.db"
    admin_api_key: str = "change-this-admin-key"
    order_action_secret: str = "change-this-order-action-secret"
    max_upload_mb: int = 30

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "sales_knowledge"

    embedding_provider: str = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_dimensions: int = 384

    llm_provider: str = "mock"
    llm_model: str = "gpt-4.1-mini"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_json_mode: bool = True

    telegram_bot_token: str | None = None
    telegram_default_company_slug: str = "yigit-aluminium"
    telegram_app_base_url: str = "http://app:8000"
    telegram_public_base_url: str | None = None
    telegram_poll_timeout_seconds: int = 30
    telegram_state_path: Path | None = None

    scraper_user_agent: str = "OMDI-Sales-RAG/0.1 (+company-knowledge-indexer)"
    scraper_timeout_seconds: int = 20
    scraper_max_response_mb: int = 5
    scraper_allow_private_networks: bool = False

    order_webhook_url: str | None = None
    order_webhook_secret: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool = True

    allowed_upload_extensions: set[str] = Field(
        default_factory=lambda: {
            ".pdf",
            ".docx",
            ".xlsx",
            ".csv",
            ".txt",
            ".md",
            ".html",
            ".htm",
            ".json",
        }
    )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "outbox").mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
