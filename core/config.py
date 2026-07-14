"""Application settings loaded from environment variables (Pydantic v2)."""
import os
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    Pydantic v2 / pydantic-settings v2 automatically maps environment
    variables to fields by name (case-insensitive), so no `env=` kwarg
    is needed on `Field()` (that was Pydantic v1 syntax and raises a
    TypeError under pydantic-settings 2.x).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    BOT_TOKEN: str
    DATABASE_URL: str
    MAX_FILE_SIZE: int = 50 * 1024 * 1024  # 50 MB default
    TEMP_FOLDER: str = "/tmp/telegram_bot"
    LOG_LEVEL: str = "INFO"
    REDIS_URL: Optional[str] = None  # reserved for future distributed throttling/FSM storage

    # Timeouts (seconds)
    LIBREOFFICE_TIMEOUT: int = 60
    OCR_TIMEOUT: int = 120
    SUBPROCESS_TIMEOUT: int = 60

    # Concurrency caps for CPU/RAM-heavy operations (OCR, bg-removal, LibreOffice)
    MAX_CONCURRENT_HEAVY_JOBS: int = 2

    # Upload limits
    MAX_FILES_PER_BATCH: int = 20

    # Railway injects PORT dynamically; 8000 is only a local-dev fallback.
    PORT: int = 8000

    @field_validator("TEMP_FOLDER")
    @classmethod
    def ensure_temp_folder(cls, v: str) -> str:
        """Create temp folder if it doesn't exist."""
        os.makedirs(v, exist_ok=True)
        return v

    @field_validator("MAX_FILE_SIZE", "MAX_FILES_PER_BATCH", "MAX_CONCURRENT_HEAVY_JOBS")
    @classmethod
    def ensure_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v


settings = Settings()

