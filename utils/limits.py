"""Centralized permission/limit system.

This module is the SINGLE source of truth for "how much of X may this user
do". Nothing else in the codebase should hardcode a limit or scatter its own
`is_owner(...)` size/count check -- handlers and services call
`get_effective_limits()` (or the `can_bypass_limits()` shortcut) and read
whatever field they need from the returned `UserLimits`.

Design:
- The bot owner (utils.permissions.is_owner) ALWAYS gets `unlimited=True` and
  every field set to `None`, which callers must treat as "no cap". The owner
  is only ever constrained by Telegram's own API limits, RAM, CPU, or disk --
  never by anything in this module.
- Every other user gets, per feature, whichever is set: their personal
  per-user override stored on the `User` row (set via the owner-only
  `/upgrade` command), or the global default from `core.config.settings` if
  they have no override.
- `FEATURE_LIMITS` is the registry mapping a short feature name (used by
  `/upgrade <user_id> <feature> <limit>`) to the `User` column that stores
  that feature's override and the `Settings` attribute holding its default.
  Adding a new limited feature means adding one entry here plus one nullable
  column on `User` -- nothing else needs to change for `/upgrade` to reach it.
"""
from dataclasses import dataclass
from typing import Optional

from core.config import settings
from utils.permissions import is_owner

# NOTE: kept for callers that only need a plain yes/no instead of the full
# limits object (e.g. deciding whether to skip a confirmation prompt).
def can_bypass_limits(user_id: int) -> bool:
    """True if this user bypasses every limit in the bot (the owner)."""
    return is_owner(user_id)


@dataclass(frozen=True)
class UserLimits:
    """Effective limits for one user for this request. Every field is
    `None` when `unlimited` is True (owner) -- callers must check `unlimited`
    or treat `None` as "no cap", never as "0" or "not configured".
    """
    unlimited: bool
    file_size: Optional[int]              # bytes, applies to every single-file upload
    batch_limit: Optional[int]            # generic "files per batch" (Image->PDF upload phase, etc.)
    pdf_queue_limit: Optional[int]        # Merge PDF queue size
    archive_compress_limit: Optional[int] # max files combined into one archive
    archive_extract_return_limit: Optional[int]  # above this many extracted files, bundle into one zip instead of individual sends
    image_to_pdf_limit: Optional[int]     # max images combined into one PDF
    pdf_split_limit: Optional[int]        # max output groups from Split
    ocr_timeout: Optional[int]            # seconds
    subprocess_timeout: Optional[int]     # seconds (ghostscript, etc.)
    libreoffice_timeout: Optional[int]    # seconds


# Registry: feature name (as used in /upgrade) -> (User column, Settings default attr)
FEATURE_LIMITS = {
    "file_size": ("file_size_limit", "MAX_FILE_SIZE"),
    "batch": ("batch_limit", "MAX_FILES_PER_BATCH"),
    "merge": ("pdf_queue_limit", "DEFAULT_PDF_QUEUE_LIMIT"),
    "archive_compress": ("archive_compress_limit", "DEFAULT_ARCHIVE_COMPRESS_LIMIT"),
    "archive_extract": ("archive_extract_return_limit", "DEFAULT_ARCHIVE_EXTRACT_RETURN_LIMIT"),
    "image_to_pdf": ("image_to_pdf_limit", "DEFAULT_IMAGE_TO_PDF_LIMIT"),
    "split": ("pdf_split_limit", "DEFAULT_PDF_SPLIT_LIMIT"),
}


def _pick(column_value: Optional[int], default: int) -> int:
    return column_value if column_value is not None else default


def get_effective_limits(user_id: int, db_user=None) -> UserLimits:
    """Return the effective `UserLimits` for `user_id`.

    `db_user` should be the `User` row for this telegram_id if you have it
    (from the database middleware); pass `None` if unavailable and every
    feature falls back to the global default (a brand-new user who hasn't
    hit `get_or_create` yet gets the same defaults as one who has).
    """
    if is_owner(user_id):
        return UserLimits(
            unlimited=True,
            file_size=None,
            batch_limit=None,
            pdf_queue_limit=None,
            archive_compress_limit=None,
            archive_extract_return_limit=None,
            image_to_pdf_limit=None,
            pdf_split_limit=None,
            ocr_timeout=None,
            subprocess_timeout=None,
            libreoffice_timeout=None,
        )

    def col(name: str) -> Optional[int]:
        return getattr(db_user, name, None) if db_user is not None else None

    return UserLimits(
        unlimited=False,
        file_size=_pick(col("file_size_limit"), settings.MAX_FILE_SIZE),
        batch_limit=_pick(col("batch_limit"), settings.MAX_FILES_PER_BATCH),
        pdf_queue_limit=_pick(col("pdf_queue_limit"), settings.DEFAULT_PDF_QUEUE_LIMIT),
        archive_compress_limit=_pick(col("archive_compress_limit"), settings.DEFAULT_ARCHIVE_COMPRESS_LIMIT),
        archive_extract_return_limit=_pick(
            col("archive_extract_return_limit"), settings.DEFAULT_ARCHIVE_EXTRACT_RETURN_LIMIT
        ),
        image_to_pdf_limit=_pick(col("image_to_pdf_limit"), settings.DEFAULT_IMAGE_TO_PDF_LIMIT),
        pdf_split_limit=_pick(col("pdf_split_limit"), settings.DEFAULT_PDF_SPLIT_LIMIT),
        # Timeouts aren't (yet) per-user overridable, but they live here too
        # so every "how long may this run" question goes through one place.
        ocr_timeout=settings.OCR_TIMEOUT,
        subprocess_timeout=settings.SUBPROCESS_TIMEOUT,
        libreoffice_timeout=settings.LIBREOFFICE_TIMEOUT,
    )


def format_limit(value: Optional[int]) -> str:
    """Human-readable rendering of a limit field for status/help messages."""
    return "unlimited" if value is None else str(value)
  
