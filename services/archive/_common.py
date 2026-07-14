"""Shared helpers for archive services."""
import os

from services.security.validator import ArchiveSecurityError


class ArchiveProcessingError(RuntimeError):
    """Raised for any expected archive processing failure."""


def safe_member_name(name: str) -> str:
    """Validate an archive member's path and return it normalized to a
    safe relative path.

    Rejects (raises ArchiveSecurityError) rather than silently stripping
    any '..' component, leading '/', or Windows drive letter. Silently
    sanitizing a traversal attempt out of the name would make the
    downstream is_within_directory() check meaningless (a sanitized path
    can never fail it) and would hide that the archive was crafted
    maliciously; rejecting the whole archive is both safer and gives an
    honest signal instead of quietly "fixing" attacker-controlled input.
    """
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise ArchiveSecurityError(f"Archive entry has an absolute path: {name!r}")

    parts = normalized.split("/")
    clean_parts = [p for p in parts if p not in ("", ".")]
    if any(p == ".." for p in clean_parts):
        raise ArchiveSecurityError(f"Archive entry attempts path traversal: {name!r}")
    if not clean_parts:
        raise ArchiveSecurityError(f"Archive contains an unsafe entry name: {name!r}")

    return os.path.join(*clean_parts)
