"""Shared helpers used by every PDF Toolkit sub-module: download/validate,
send/cleanup, error handling, and small formatting utilities.
"""
import asyncio
from typing import List, Optional

from aiogram.types import Message, FSInputFile
from aiogram.fsm.context import FSMContext

from core.logger import logger
from utils.limits import get_effective_limits

from services.pdf._common import PDFProcessingError

from utils.tempfiles import (
    new_temp_path,
    track_temp_file,
    untrack_temp_files,
    delete_paths,
)
from utils.validators import validate_extension, validate_upload

_PDF_MIME = {"application/pdf"}
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif"}

_QUEUE_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"
_DISPLAY_NAME_MAX = 42  # visible chars before "...pdf", per the filename-shortening spec


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

async def _track_usage(user_repo, db_user) -> None:
    if user_repo is not None and db_user is not None:
        try:
            await user_repo.increment_usage(db_user.telegram_id)
        except Exception:
            logger.exception("Failed to increment usage counter (non-fatal).")


async def _download_and_validate(
    message: Message,
    state: FSMContext,
    allowed_extensions: set,
    allowed_mime_types: set,
    kind_label: str,
    db_user=None,
) -> Optional[str]:
    """Download the document attached to `message`, validate it, and track
    it for cleanup. Returns the temp path on success; on failure, replies
    with a user-facing error and returns None.

    File-size limit is read from the caller's centralized effective limits
    (utils.limits.get_effective_limits) -- the owner always gets `file_size
    is None`, meaning no cap, here and everywhere else in the bot.
    """
    doc = message.document
    if doc is None:
        await message.answer(
            f"Please send a {kind_label} file as a document (not a photo)."
        )
        return None

    limits = get_effective_limits(message.from_user.id, db_user)

    if not limits.unlimited:
        if doc.file_size and doc.file_size > limits.file_size:
            limit_mb = limits.file_size // (1024 * 1024)
            await message.answer(f"File too large (max {limit_mb} MB).")
            return None

    if not validate_extension(doc.file_name or "", allowed_extensions):
        allowed_str = ", ".join(sorted(allowed_extensions))
        await message.answer(
            f"Unsupported file type. Allowed: {allowed_str}"
        )
        return None

    suffix = (
        "." + (doc.file_name or "").rsplit(".", 1)[-1].lower()
        if "." in (doc.file_name or "")
        else ""
    )

    temp_path = new_temp_path(suffix=suffix)
    await track_temp_file(state, temp_path)

    try:
        await message.bot.download(doc, destination=temp_path)
    except Exception as e:
        logger.error(f"Download failed: {e}")
        await untrack_temp_files(state, [temp_path])
        delete_paths([temp_path])
        await message.answer(
            "Failed to download the file from Telegram. Please try again."
        )
        return None

    error = validate_upload(
        temp_path,
        doc.file_name or f"file{suffix}",
        allowed_extensions=allowed_extensions,
        allowed_mime_types=allowed_mime_types,
        max_size=limits.file_size,
    )

    if error:
        await untrack_temp_files(state, [temp_path])
        delete_paths([temp_path])
        await message.answer(error)
        return None

    return temp_path


async def _finish_with_document(
    message: Message,
    state: FSMContext,
    output_path: str,
    filename: str,
    cleanup_paths: List[str],
    caption: Optional[str] = None,
) -> None:
    """Send one output file to the user, then always clean up every path in
    `cleanup_paths` (inputs + output), whether sending succeeded or not.
    """
    try:
        await message.answer_document(FSInputFile(output_path, filename=filename), caption=caption)
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()


async def _finish_with_documents(
    message: Message,
    state: FSMContext,
    output_paths: List[str],
    filename_fn,
    cleanup_paths: List[str],
) -> None:
    """Send multiple output files (e.g. Split, PDF->Images), then always
    clean up every tracked path regardless of how many sends succeeded.
    """
    try:
        for i, path in enumerate(output_paths, start=1):
            await message.answer_document(FSInputFile(path, filename=filename_fn(i)))
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()


async def _fail(message: Message, state: FSMContext, error: Exception, cleanup_paths: List[str]) -> None:
    """Handle a processing failure: tell the user, clean up temp files, and
    leave the flow (rather than getting stuck in a dead state).
    """
    if isinstance(error, PDFProcessingError):
        await message.answer(f"⚠️ {error}")
    else:
        logger.exception(f"Unexpected PDF processing error: {error}")
        await message.answer("Something went wrong processing that file. Please try again.")
    delete_paths(cleanup_paths)
    await untrack_temp_files(state, cleanup_paths)
    await state.clear()


def _format_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "? MB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _display_name(name: str) -> str:
    """Shorten only for display -- the real filename is never touched.
    Keeps the extension, truncates the stem to fit inside ~40-45 visible
    characters total.
    """
    if len(name) <= _DISPLAY_NAME_MAX:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    keep = max(_DISPLAY_NAME_MAX - len(ext) - 4, 5)  # room for "...", ".", ext
    short_stem = stem[:keep].rstrip()
    return f"{short_stem}...{('.' + ext) if ext else ''}"


_VALIDATION_ERROR_TTL_SECONDS = 7


async def _send_temp_validation_error(bot, chat_id: int, text: str) -> None:
    """Send a validation error message and auto-delete it after ~7s.
    Purely cosmetic (UI polish) -- never touches queue/FSM state, and any
    failure to send or delete is swallowed so it can never break the
    actual flow.
    """
    try:
        sent = await bot.send_message(chat_id, text)
    except Exception as e:
        logger.debug(f"Could not send temporary validation error: {e}")
        return

    async def _delete_later():
        await asyncio.sleep(_VALIDATION_ERROR_TTL_SECONDS)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
        except Exception as e:
            logger.debug(f"Could not auto-delete validation error: {e}")

    asyncio.create_task(_delete_later())
