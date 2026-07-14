"""Startup validation + shared security primitives used by every service
that touches user-supplied files: safe subprocess execution and safe
archive extraction (Zip Slip / Zip Bomb protection).
"""
import asyncio
import os
import shutil
from typing import List, Optional, Tuple

from core.config import settings
from core.logger import logger

# Binaries the various services shell out to. Missing ones don't crash the
# bot (that feature is simply unavailable), but we want a clear startup log
# rather than a confusing failure the first time a user tries the feature.
_EXPECTED_BINARIES = {
    "ghostscript": "gs",
    "libreoffice": "soffice",
    "tesseract-ocr": "tesseract",
    "rar-extraction (unar)": "unar",
}


def init_validator() -> None:
    """Run at application startup. Verifies libmagic is loadable and reports
    which optional external tools are present so missing system
    dependencies show up as a clear log line, not a runtime KeyError deep
    inside a handler.
    """
    try:
        import magic
        magic.Magic(mime=True)
        logger.info("libmagic loaded OK.")
    except Exception as e:
        logger.error(
            f"libmagic failed to initialize ({e}). MIME validation will "
            f"reject every file until this is fixed (install libmagic1)."
        )

    for label, binary in _EXPECTED_BINARIES.items():
        path = shutil.which(binary)
        if path:
            logger.info(f"Found {label}: {path}")
        else:
            logger.warning(
                f"{label} binary '{binary}' not found on PATH -- features "
                f"depending on it will be unavailable until it's installed."
            )

    os.makedirs(settings.TEMP_FOLDER, exist_ok=True)
    logger.info(f"Temp folder ready: {settings.TEMP_FOLDER}")


# --------------------------------------------------------------------------
# Safe subprocess execution
# --------------------------------------------------------------------------

class SubprocessError(RuntimeError):
    """Raised when an external tool invocation fails or times out."""


async def run_subprocess_safe(
    cmd: List[str],
    timeout: Optional[int] = None,
    cwd: Optional[str] = None,
) -> Tuple[str, str]:
    """Run an external command safely: argv list only (never shell=True, so
    there is no shell-injection surface), with an enforced timeout that
    kills the process group on expiry instead of leaving it running.

    Returns (stdout, stderr) as text. Raises SubprocessError on non-zero
    exit or timeout.
    """
    if not cmd or not isinstance(cmd, list):
        raise ValueError("cmd must be a non-empty list of argv strings")

    timeout = timeout or settings.SUBPROCESS_TIMEOUT
    logger.debug(f"Running subprocess: {cmd!r} (timeout={timeout}s)")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SubprocessError(f"Command timed out after {timeout}s: {cmd[0]}")

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise SubprocessError(
            f"Command '{cmd[0]}' failed (exit {proc.returncode}): {stderr[:500]}"
        )

    return stdout, stderr


# --------------------------------------------------------------------------
# Safe archive extraction (Zip Slip / Zip Bomb protection)
# --------------------------------------------------------------------------

class ArchiveSecurityError(RuntimeError):
    """Raised when an archive fails a safety check (path traversal, bomb, etc.)."""


def is_within_directory(directory: str, target: str) -> bool:
    """True if `target` resolves to a path inside `directory` (prevents
    Zip Slip: a malicious archive member named e.g. '../../etc/passwd').
    """
    abs_directory = os.path.realpath(directory)
    abs_target = os.path.realpath(target)
    return os.path.commonpath([abs_directory]) == os.path.commonpath([abs_directory, abs_target])


def check_archive_bomb(
    member_sizes: List[int],
    compressed_size: int,
    max_files: int = 2000,
    max_total_uncompressed: int = 1024 * 1024 * 1024,  # 1 GB
    max_ratio: int = 200,
) -> None:
    """Raise ArchiveSecurityError if an archive's declared metadata looks
    like a decompression bomb. Call this BEFORE extracting any bytes, using
    the sizes reported in the archive's central directory/header.
    """
    if len(member_sizes) > max_files:
        raise ArchiveSecurityError(
            f"Archive has too many entries ({len(member_sizes)} > {max_files})."
        )

    total_uncompressed = sum(member_sizes)
    if total_uncompressed > max_total_uncompressed:
        raise ArchiveSecurityError(
            f"Archive's uncompressed size ({total_uncompressed} bytes) exceeds the limit."
        )

    if compressed_size > 0:
        ratio = total_uncompressed / max(compressed_size, 1)
        if ratio > max_ratio:
            raise ArchiveSecurityError(
                f"Archive compression ratio ({ratio:.0f}x) exceeds the safety limit "
                f"({max_ratio}x) -- likely a decompression bomb."
            )
          
