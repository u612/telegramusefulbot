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

    # --- Owner / admin ---
    # Telegram numeric user id of the bot owner. 0 means "unset" -- no user
    # gets owner privileges (unlimited merge queue, /upgrade, /userbot_on,
    # /userbot_off, /status).
    OWNER_ID: int = 0

    # --- Merge: default queue size for non-upgraded users ---
    DEFAULT_PDF_QUEUE_LIMIT: int = 20

    # --- Optional userbot (MTProto) transport for large files ---
    # All three must be set for userbot mode to be available at all; even
    # then it stays OFF until the owner runs /userbot_on (see
    # database.repositories.BotConfigRepository). This keeps a normal
    # Bot-API-only deployment completely unaffected if these are left blank.
    USERBOT_API_ID: Optional[int] = None
    USERBOT_API_HASH: Optional[str] = None
    USERBOT_SESSION_STRING: Optional[str] = None

    # The public Telegram Bot API cannot download files above ~20 MB via
    # getFile regardless of MAX_FILE_SIZE below -- this is a Telegram-side
    # limit, not ours. Files at or above this size can only be fetched
    # through the userbot transport. Kept slightly under the real 20 MB
    # ceiling as a safety margin.
    BOT_API_SAFE_DOWNLOAD_LIMIT: int = 19 * 1024 * 1024

    # Ceiling for merge input/output file size when the userbot transport is
    # active. When the userbot is not enabled, MAX_FILE_SIZE (Bot-API path)
    # is still the effective ceiling.
    MAX_FILE_SIZE_USERBOT: int = 200 * 1024 * 1024

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
