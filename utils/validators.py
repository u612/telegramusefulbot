"""Reusable validation helpers: extension, size, MIME/magic-byte checks,
and filename sanitization to prevent path traversal.
"""
import os
import re
from typing import Optional

import magic

from core.config import settings
from core.constants import ALLOWED_MIME_TYPES
from core.logger import logger


def validate_file_size(file_path: str, max_size: Optional[int] = None) -> bool:
    """Check if a file on disk is within the size limit.

    Defaults to the runtime-configurable `settings.MAX_FILE_SIZE` so there
    is a single source of truth (do not duplicate this constant elsewhere).
    """
    limit = max_size if max_size is not None else settings.MAX_FILE_SIZE
    size = os.path.getsize(file_path)
    return size <= limit


def validate_extension(filename: str, allowed_extensions: set) -> bool:
    """Check if a filename's extension is in the allowed set (case-insensitive)."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in allowed_extensions


def validate_mime(file_path: str, allowed_mime_types: Optional[set] = None) -> Optional[str]:
    """Detect the real MIME type from file content (magic bytes), independent
    of the filename/extension a user claims. Returns the MIME type string if
    it is allowed, otherwise None.

    This is deliberately separate from `validate_extension`: a filename
    check alone can be trivially spoofed (rename `evil.exe` to `evil.pdf`).
    Both checks should be used together -- extension for a fast/cheap
    rejection, magic bytes as the authoritative check before processing.
    """
    allowed = allowed_mime_types if allowed_mime_types is not None else ALLOWED_MIME_TYPES
    try:
        mime = magic.from_file(file_path, mime=True)
    except Exception as e:
        logger.error(f"Error detecting MIME type for {file_path}: {e}")
        return None

    if mime in allowed:
        return mime

    logger.warning(f"Rejected file with unsupported MIME type: {mime} ({file_path})")
    return None


_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_.-]")


def sanitize_filename(filename: str) -> str:
    """Produce a filesystem-safe filename with no path separators or
    traversal sequences. Always use this for any user-supplied filename
    that will be joined onto a directory path; never trust `message.document.file_name`
    directly for that purpose.
    """
    # Strip any directory component the client might have sent.
    filename = os.path.basename(filename or "")
    safe = _SAFE_FILENAME_RE.sub("_", filename)
    safe = safe.lstrip(".")  # avoid dotfiles / bare ".."
    safe = safe.replace("..", "_")
    if not safe:
        safe = "file"
    # Keep filenames from growing unbounded (some clients allow very long names).
    return safe[:200]


def validate_upload(
    file_path: str,
    original_filename: str,
    allowed_extensions: set,
    allowed_mime_types: Optional[set] = None,
    max_size: Optional[int] = None,
) -> Optional[str]:
    """Run the full validation pipeline (extension -> size -> magic bytes)
    on an already-downloaded file. Returns an error message string if
    validation fails, or None if the file passes every check.
    """
    if not validate_extension(original_filename, allowed_extensions):
        allowed_str = ", ".join(sorted(allowed_extensions))
        return f"Unsupported file type. Allowed: {allowed_str}"

    if not validate_file_size(file_path, max_size):
        limit = (max_size if max_size is not None else settings.MAX_FILE_SIZE) // (1024 * 1024)
        return f"File too large (max {limit} MB)."

    mime = validate_mime(file_path, allowed_mime_types)
    if mime is None:
        return "File content doesn't match a supported file type (failed content validation)."

    return None
