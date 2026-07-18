"""Handlers for every PDF Toolkit operation: Merge, Split, Compress, Rotate,
Extract, Rearrange, Watermark, Add/Remove Password, Image<->PDF.

Design notes (see the audit for the bugs this fixes):
- Every downloaded file is registered via `track_temp_file(state, path)`
  immediately after it's written to disk. base.py's Back/Home/Cancel/
  /start handlers sweep up anything still tracked, so an abandoned flow
  can no longer leak temp files forever.
- Every processing function's output is deleted in a `finally` block after
  it's sent (or after a send failure), not only on the success path.
- Multi-file flows (Merge, Image->PDF) reuse the FSM-tracked temp file list
  itself as the input list, so there's a single source of truth for "what
  has the user uploaded so far".
"""
import asyncio
import os
import time
from typing import Dict, List, Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.states.pdf import PDFStates
from bot.keyboards.pdf import (
    get_pdf_menu,
    PDF_MERGE, PDF_SPLIT, PDF_COMPRESS, PDF_ROTATE, PDF_EXTRACT,
    PDF_REARRANGE, PDF_WATERMARK, PDF_ADD_PASSWORD, PDF_REMOVE_PASSWORD,
    PDF_IMAGE_TO_PDF, PDF_PDF_TO_IMAGES, PDF_DONE,
    upload_done_keyboard, rotate_angle_keyboard,
    pdf_to_images_format_keyboard, merge_queue_keyboard,
    PDF_ROTATE_90, PDF_ROTATE_180, PDF_ROTATE_270,
    PDF_TO_IMG_PNG, PDF_TO_IMG_JPEG,
)
from bot.keyboards.common import back_home_cancel
from core.constants import (
    CB_PDF, SUPPORTED_PDF_EXTS, SUPPORTED_IMAGE_EXTS, ALLOWED_MIME_TYPES,
    MERGE_PROGRESS_EDIT_INTERVAL_SECONDS, MERGE_BATCH_FINALIZE_DELAY_SECONDS,
)
from core.config import settings
from core.logger import logger
from utils.limits import get_effective_limits
from services.telegram import get_transport

from services.pdf.merger import PDFMerger
from services.pdf.splitter import PDFSplitter
from services.pdf.compressor import PDFCompressor, CompressionMode
from services.pdf.rotator import PDFRotator
from services.pdf.extractor import PDFExtractor
from services.pdf.rearranger import PDFRearranger
from services.pdf.watermark import PDFWatermark
from services.pdf.password import PDFPassword
from services.pdf.image_to_pdf import ImageToPDF
from services.pdf.pdf_to_images import PDFToImages
from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count

from utils.tempfiles import (
    new_temp_path,
    track_temp_file,
    untrack_temp_files,
    get_tracked_files,
    delete_paths,
)
from utils.validators import validate_extension, validate_upload, sanitize_filename

router = Router()

_PDF_MIME = {"application/pdf"}
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif"}

# --------------------------------------------------------------------------
# Merge queue: single-process, in-memory batch state
# --------------------------------------------------------------------------
# Architecture (correctness over speed, per explicit request):
#
# Telegram delivers a "media group" (several PDFs sent together) as a rapid
# burst of separate Message updates, not one atomic update, and there's no
# signal that says "the album is complete". So instead of downloading and
# committing each file to the queue as it arrives:
#
#   1. Receiving a document ONLY buffers a reference to it (in arrival
#      order) in memory -- no download yet, nothing added to the queue yet.
#   2. The very first file of a fresh burst sends one "Receiving your
#      PDFs..." status message immediately, for visual feedback. That
#      message is never edited again until the whole burst is processed --
#      no partial counts, no partial queue, ever.
#   3. Every new file reschedules a short debounce timer. Once the chat has
#      been quiet for MERGE_BATCH_FINALIZE_DELAY_SECONDS, the burst is
#      considered finished.
#   4. Only then are the buffered files downloaded and validated, ONE AT A
#      TIME, in the exact order they were buffered, each committed to the
#      FSM-tracked queue (get_tracked_files/track_temp_file) before moving
#      to the next.
#   5. The status message is edited, once, into the final queue view.
#
# All of this -- buffering, downloading, and every read/write of the
# committed queue -- runs under one per-chat asyncio.Lock (get_merge_lock),
# shared with Done, the actual merge step, and Cancel/Back/Home/a fresh
# /start (see base.py's `_reset_to_main_menu`). That's what guarantees a
# file already buffered is never lost, never duplicated, and never
# reordered, no matter how Done/Cancel/a new burst interleave with it.


class _MergeBatch:
    """Per-chat, in-memory state for one merge flow's not-yet-committed
    uploads. Deliberately NOT stored in FSM data: it holds live aiogram
    Message objects (needed to actually download each file later) and an
    asyncio.Task, neither of which belong in serializable FSM state. Safe
    as plain in-memory state because the bot runs single-process with
    aiogram's MemoryStorage already (same assumption the rest of Merge's
    state relies on).
    """

    __slots__ = ("pending", "seen_file_unique_ids", "task")

    def __init__(self):
        # Ordered list of (message, size_ceiling) tuples, exactly in the
        # order Telegram delivered them -- this list IS the order guarantee;
        # nothing downstream ever reorders it.
        self.pending: List[tuple] = []
        self.seen_file_unique_ids: set = set()
        self.task: Optional[asyncio.Task] = None


_merge_batches: Dict[int, _MergeBatch] = {}


def _get_merge_batch(chat_id: int) -> _MergeBatch:
    batch = _merge_batches.get(chat_id)
    if batch is None:
        batch = _MergeBatch()
        _merge_batches[chat_id] = batch
    return batch


_merge_locks: Dict[int, asyncio.Lock] = {}


def get_merge_lock(chat_id: int) -> asyncio.Lock:
    """The single per-chat lock that serializes every piece of code that
    touches a chat's merge queue: buffering a file, downloading a finished
    burst, pressing Done, actually performing the merge, and resetting/
    cancelling the flow (Cancel/Back/Home/a fresh /start -- see base.py's
    `_reset_to_main_menu`). Giving all of those one shared lock is what
    makes "Done pressed while uploads are still arriving" and "Cancel
    pressed mid-upload" safe: whichever one grabs the lock first runs to
    completion before the other can see or mutate the queue state, so a
    buffered/downloading upload can never be silently dropped, double
    counted, reordered, or written into a queue that has already moved on.
    """
    lock = _merge_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _merge_locks[chat_id] = lock
    return lock


def cancel_pending_merge_batch(chat_id: int) -> None:
    """Cancel any pending merge-batch finalize task, discard any buffered
    (not-yet-downloaded) files, and drop this chat's merge lock from the
    registry. Called whenever the merge flow leaves the "waiting for
    files" stage -- Done, Cancel, Back, Home, or a fresh /start.

    Always called by a caller that is itself holding (a local reference
    to) that very lock, so dropping it from the dict here is safe: any
    task still waiting on it holds its own reference and is unaffected,
    and the next merge flow to touch this chat simply gets a fresh,
    uncontended lock. Discarding `pending` here is safe too -- nothing in
    it has been downloaded to disk yet, so there's nothing to clean up.
    """
    batch = _merge_batches.pop(chat_id, None)
    if batch and batch.task and not batch.task.done():
        batch.task.cancel()
    _merge_locks.pop(chat_id, None)


# --------------------------------------------------------------------------
# Main menu entry point
# --------------------------------------------------------------------------

@router.callback_query(F.data == CB_PDF)
async def pdf_menu_open(query: CallbackQuery, state: FSMContext):
    """Opens the PDF submenu from the main menu's "📄 PDF" button."""
    await query.message.edit_text(
        "📄 PDF Toolkit -- choose an operation:",
        reply_markup=get_pdf_menu(),
    )
    await query.answer()


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

# --------------------------------------------------------------------------
# Merge queue UI
# --------------------------------------------------------------------------

_QUEUE_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"
_DISPLAY_NAME_MAX = 42  # visible chars before "...pdf", per the filename-shortening spec

# NOTE: This flow needs two additional FSM states beyond what already
# existed (waiting_for_files_merge, waiting_for_merge_filename):
#
#   PDFStates.waiting_for_merge_arrange   -- waiting for the user to type
#                                             the new file order (e.g. "3,1,2")
#   PDFStates.waiting_for_merge_preview   -- waiting for Confirm / Rearrange
#                                             Again / Add More / Home / Cancel
#
# Add these two members to bot/states/pdf.py's PDFStates alongside the
# existing merge states -- nothing else in that file needs to change.

# Merge-flow-local callback data. Deliberately NOT reusing the generic
# CB_BACK/CB_HOME/CB_CANCEL from back_home_cancel() -- Cancel here always
# needs a confirmation step first (spec: "required on EVERY Merge screen"),
# and Home needs to run Merge-specific cleanup. Keeping these string
# constants local to this section avoids touching bot/keyboards/pdf.py or
# core/constants.py.
MERGE_CB_CANCEL = "pdfmerge:cancel"                # opens the confirmation screen
MERGE_CB_CANCEL_YES = "pdfmerge:cancel_yes"
MERGE_CB_CANCEL_NO = "pdfmerge:cancel_no"
MERGE_CB_PROCEED_CURRENT_ORDER = "pdfmerge:proceed_current_order"
MERGE_CB_CONFIRM = "pdfmerge:confirm"
MERGE_CB_REARRANGE_AGAIN = "pdfmerge:rearrange_again"
MERGE_CB_ADD_MORE = "pdfmerge:add_more"

_MERGE_STATES = (
    PDFStates.waiting_for_files_merge,
    PDFStates.waiting_for_merge_arrange,
    PDFStates.waiting_for_merge_preview,
    PDFStates.waiting_for_merge_filename,
)

# The "screen state" a chat is on is fully described by (fsm_state,
# merge_add_more_mode). Cancel's "No, Continue" needs to restore exactly
# that screen -- see pdf_merge_cancel_no below.


def _merge_upload_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Done", callback_data=PDF_DONE)
    b.button(text="❌ Cancel", callback_data=MERGE_CB_CANCEL)
    b.adjust(2)
    return b.as_markup()


def _merge_arrange_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Proceed with This Order", callback_data=MERGE_CB_PROCEED_CURRENT_ORDER)
    b.button(text="➕ Add More", callback_data=MERGE_CB_ADD_MORE)
    b.button(text="❌ Cancel", callback_data=MERGE_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


def _merge_preview_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm", callback_data=MERGE_CB_CONFIRM)
    b.button(text="🔄 Rearrange Again", callback_data=MERGE_CB_REARRANGE_AGAIN)
    b.button(text="➕ Add More", callback_data=MERGE_CB_ADD_MORE)
    b.button(text="❌ Cancel", callback_data=MERGE_CB_CANCEL)
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def _merge_filename_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=MERGE_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _merge_cancel_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yes, Cancel", callback_data=MERGE_CB_CANCEL_YES)
    b.button(text="❎ No, Continue", callback_data=MERGE_CB_CANCEL_NO)
    b.adjust(2)
    return b.as_markup()


def _format_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "? MB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _format_total_size(sizes: List[Optional[int]]) -> str:
    total = sum(s for s in sizes if s)
    return _format_size(total) if total else "? MB"


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


def _render_receiving_text() -> str:
    """Shown immediately on the first file of a burst, and left untouched
    until the whole burst has been downloaded and committed -- never a
    running count, never a partial queue.
    """
    return (
        f"{_QUEUE_DIVIDER}\n"
        "📄 Merge PDF\n\n"
        "⏳ Receiving your PDFs...\n\n"
        "Please wait while all files are detected.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_queue_updated_text(count: int, sizes: List[Optional[int]], failed: Optional[List[str]] = None, add_more: bool = False) -> str:
    lines = [
        _QUEUE_DIVIDER,
        "📄 Add More PDFs" if add_more else "📄 Merge Queue",
        "",
        f"📁 Files: {count}",
        f"💾 Total Size: {_format_total_size(sizes)}",
        "",
    ]
    if count > 0:
        lines.append("Upload more PDFs")
        lines.append("or press Done.")
    else:
        lines.append("No PDFs were added.")
        lines.append("")
        lines.append("Send PDF files to build a queue.")
    lines.append(_QUEUE_DIVIDER)
    text = "\n".join(lines)
    if failed:
        shown = ", ".join(failed[:5])
        more = "" if len(failed) <= 5 else f", and {len(failed) - 5} more"
        text += f"\n\n⚠️ Skipped {len(failed)} file(s) that failed to download or weren't valid PDFs: {shown}{more}"
    return text


def _render_arrange_text(names: List[str], sizes: List[Optional[int]], order: List[int]) -> str:
    lines = [_QUEUE_DIVIDER, "📄 Arrange PDF Order", "", "Current Order", ""]
    for pos, idx in enumerate(order, start=1):
        lines.append(f"{pos}. {_display_name(names[idx])} ({_format_size(sizes[idx])})")
    lines += [
        "",
        _QUEUE_DIVIDER,
        "",
        "📝 Send the new order using numbers.",
        "Example:",
        "3,1,2",
    ]
    return "\n".join(lines)


def _render_preview_text(names: List[str], sizes: List[Optional[int]], order: List[int]) -> str:
    lines = [_QUEUE_DIVIDER, "✅ New Merge Order", ""]
    for pos, idx in enumerate(order, start=1):
        lines.append(f"{pos}. {_display_name(names[idx])} ({_format_size(sizes[idx])})")
    lines += ["", _QUEUE_DIVIDER, "", "Is this correct?"]
    return "\n".join(lines)


def _parse_merge_order(text: str, count: int):
    """Parse a comma-separated (spaces allowed) 1-based order string.

    Returns (zero_based_order, error_message). On success error_message is
    None. On failure zero_based_order is None and error_message is a
    friendly, user-facing string -- the caller must re-ask without
    clearing the queue.
    """
    parts = [p.strip() for p in text.split(",") if p.strip() != ""]
    if not parts:
        return None, "Please send the order as numbers separated by commas, e.g. 3,1,2."
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None, "That doesn't look like a list of numbers. Please send something like 3,1,2."
    if len(nums) != count:
        return None, f"I need exactly {count} numbers (one per file), but got {len(nums)}. Please try again."
    if len(set(nums)) != len(nums):
        return None, "Each number must appear exactly once -- no duplicates. Please try again."
    if sorted(nums) != list(range(1, count + 1)):
        return None, f"Please use every number from 1 to {count} exactly once."
    return [n - 1 for n in nums], None


async def _start_new_queue_message(bot, state: FSMContext, chat_id: int, text: str, keyboard=None) -> None:
    """Delete the previous Merge Queue message (if any -- this also covers
    the initial "send your PDFs" prompt, which the first uploaded batch
    replaces) and send a fresh one. Telegram already displays the uploaded
    files themselves; this fresh message lands right after them, so the
    queue status always sits below the newest uploads instead of above
    them.
    """
    data = await state.get_data()
    old_chat_id = data.get("merge_status_chat_id")
    old_message_id = data.get("merge_status_message_id")
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Merge status delete skipped: {e}")

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard or _merge_upload_keyboard())
    await state.update_data(
        merge_status_chat_id=chat_id,
        merge_status_message_id=sent.message_id,
        merge_last_status_edit_ts=0.0,
    )


async def _edit_merge_status(
    bot,
    state: FSMContext,
    text: str,
    keyboard=None,
    force: bool = False,
) -> None:
    """Edit the single tracked queue status message in place. Never sends a
    new message -- that's the whole point ("never spam chat with many
    messages").

    Progress edits are throttled to at most once every
    MERGE_PROGRESS_EDIT_INTERVAL_SECONDS unless `force=True` (used for the
    state-changing edits: batch finalized, filename prompt, etc.), so a
    burst of files doesn't trip Telegram's flood limits.
    """
    data = await state.get_data()
    chat_id = data.get("merge_status_chat_id")
    message_id = data.get("merge_status_message_id")
    if chat_id is None or message_id is None:
        return

    last_edit = data.get("merge_last_status_edit_ts", 0.0)
    now = time.monotonic()
    if not force and (now - last_edit) < MERGE_PROGRESS_EDIT_INTERVAL_SECONDS:
        return

    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
    except Exception as e:
        # "message is not modified", message deleted, etc. -- never let a
        # status-display hiccup break the actual queue/merge logic.
        logger.debug(f"Merge status edit skipped: {e}")

    await state.update_data(merge_last_status_edit_ts=now)


_VALIDATION_ERROR_TTL_SECONDS = 7


async def _send_temp_validation_error(bot, chat_id: int, text: str) -> None:
    """Send a validation error message and auto-delete it after ~7s.
    Purely cosmetic (UI polish) -- never touches queue/FSM state, and any
    failure to send or delete is swallowed so it can never break the
    actual Merge flow.
    """
    try:
        sent = await bot.send_message(chat_id, text)
    except Exception as e:
        logger.debug(f"Merge: could not send temporary validation error: {e}")
        return

    async def _delete_later():
        await asyncio.sleep(_VALIDATION_ERROR_TTL_SECONDS)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
        except Exception as e:
            logger.debug(f"Merge: could not auto-delete validation error: {e}")

    asyncio.create_task(_delete_later())


async def _process_pending_batch(state: FSMContext, chat_id: int, bot_config_repo) -> tuple:
    """MUST be called while holding get_merge_lock(chat_id). Downloads
    every currently-buffered file, strictly in the order they were
    buffered, committing each to the FSM-tracked queue (track_temp_file +
    merge_file_names/merge_file_sizes) before moving to the next file. A
    failure on one file (download error, corrupt/invalid PDF) only skips
    that file -- it's reported back, never silently dropped, and never
    allowed to lose or reorder any other file in the batch.

    Returns (added_names, failed_names).
    """
    batch = _merge_batches.get(chat_id)
    if batch is None or not batch.pending:
        return [], []

    pending = batch.pending
    batch.pending = []  # claim this burst's items for processing

    userbot_enabled = bool(bot_config_repo) and await bot_config_repo.is_userbot_enabled()

    added: List[str] = []
    failed: List[str] = []
    for msg, size_ceiling in pending:
        doc = msg.document
        display_name = doc.file_name or "file.pdf"
        temp_path = new_temp_path(suffix=".pdf")
        await track_temp_file(state, temp_path)

        transport = await get_transport(doc.file_size or 0, userbot_enabled)
        try:
            await transport.download(msg, temp_path)
        except Exception as e:
            logger.error(f"Merge: download failed for buffered file '{display_name}': {e}")
            await untrack_temp_files(state, [temp_path])
            delete_paths([temp_path])
            failed.append(display_name)
            continue

        error = validate_upload(
            temp_path, display_name,
            allowed_extensions=SUPPORTED_PDF_EXTS, allowed_mime_types=_PDF_MIME,
            max_size=size_ceiling,
        )
        if error:
            logger.info(f"Merge: rejected invalid buffered PDF '{display_name}': {error}")
            await untrack_temp_files(state, [temp_path])
            delete_paths([temp_path])
            failed.append(display_name)
            continue

        data = await state.get_data()
        file_names: List[str] = list(data.get("merge_file_names", []))
        file_sizes: List[Optional[int]] = list(data.get("merge_file_sizes", []))
        file_names.append(display_name)
        file_sizes.append(doc.file_size)
        await state.update_data(merge_file_names=file_names, merge_file_sizes=file_sizes)
        added.append(display_name)

    return added, failed


async def _finalize_merge_batch(bot, state: FSMContext, chat_id: int, bot_config_repo) -> None:
    """Runs MERGE_BATCH_FINALIZE_DELAY_SECONDS after the most recently
    buffered file; if nothing newer has rescheduled it in the meantime,
    the burst is considered complete: download + commit every buffered
    file (in order), then do the single "Queue Updated" edit.
    """
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    async with get_merge_lock(chat_id):
        batch = _merge_batches.get(chat_id)
        if batch is None or batch.task is not asyncio.current_task():
            return  # a newer file superseded this task, or the flow ended
        batch.task = None

        if await state.get_state() != PDFStates.waiting_for_files_merge.state:
            return  # flow was cancelled/finished/navigated away in the meantime

        added, failed = await _process_pending_batch(state, chat_id, bot_config_repo)
        if not added and not failed:
            return  # nothing was actually buffered (shouldn't normally happen)

        data = await state.get_data()
        sizes = list(data.get("merge_file_sizes", []))
        total = len(sizes)
        add_more = bool(data.get("merge_add_more_mode"))
        await _edit_merge_status(
            bot, state,
            _render_queue_updated_text(total, sizes, failed, add_more=add_more),
            keyboard=_merge_upload_keyboard(),
            force=True,
        )
        logger.info(
            f"Merge: batch finalized for chat {chat_id}: "
            f"{len(added)} added, {len(failed)} failed, total={total}"
        )


# --------------------------------------------------------------------------
# Merge (buffer -> debounce -> download-in-order -> commit)
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_MERGE)
async def pdf_merge_start(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    _merge_batches.pop(chat_id, None)  # defensive: no stale buffer from a previous flow
    await state.set_state(PDFStates.waiting_for_files_merge)
    await query.message.edit_text(
        f"{_QUEUE_DIVIDER}\n"
        "📄 Merge PDF\n\n"
        "Send your PDF files.\n\n"
        "💡 Tip\n"
        "• Upload up to 9 PDFs at once.\n"
        "• If you have more, upload them in multiple batches.\n\n"
        f"{_QUEUE_DIVIDER}\n\n"
        "Queue: 0 PDFs",
        reply_markup=_merge_upload_keyboard(),
    )
    await state.update_data(
        merge_status_chat_id=chat_id,
        merge_status_message_id=query.message.message_id,
        merge_last_status_edit_ts=0.0,
        merge_file_names=[],
        merge_file_sizes=[],
        merge_order=None,
        merge_add_more_mode=False,
        merge_pre_cancel_state=None,
        merge_pre_cancel_add_more=None,
    )
    await query.answer()
    logger.info(f"Merge: queue opened for user {query.from_user.id}")


@router.message(PDFStates.waiting_for_files_merge, F.document)
async def pdf_merge_receive(message: Message, state: FSMContext, user_repo=None, bot_config_repo=None, db_user=None):
    doc = message.document
    user_id = message.from_user.id
    chat_id = message.chat.id

    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await message.answer("❌ Please send PDF files only.")
        logger.info(f"Merge: rejected non-PDF document '{doc.file_name}' from user {user_id}")
        return

    limits = get_effective_limits(user_id, db_user)

    # Everything that touches this chat's buffer or committed queue runs
    # under one lock -- see get_merge_lock's docstring. Note that unlike the
    # old design, the (slow) download itself does NOT happen inside this
    # per-file lock acquisition anymore: receiving a file only buffers a
    # reference to it, so this critical section is fast regardless of file
    # size, and the whole album gets buffered in order before any network
    # I/O for downloading starts.
    lock = get_merge_lock(chat_id)
    async with lock:
        batch = _get_merge_batch(chat_id)

        # Guard against Telegram redelivering an update (rare, but possible
        # on flaky connections/polling hiccups): dedupe by Telegram's own
        # file_unique_id, which identifies the actual file content.
        if doc.file_unique_id and doc.file_unique_id in batch.seen_file_unique_ids:
            logger.info(f"Merge: duplicate upload ignored for user {user_id} (file_unique_id={doc.file_unique_id})")
            return

        committed = await get_tracked_files(state)
        total_so_far = len(committed) + len(batch.pending)
        if not limits.unlimited and total_so_far >= limits.pdf_queue_limit:
            await message.answer(
                f"You've reached your merge queue limit of {limits.pdf_queue_limit} PDFs. "
                f"Press Done to merge, or Cancel."
            )
            return

        # Cheap, download-free size check straight from Telegram's own
        # metadata -- rejects an obviously oversized file immediately
        # instead of only discovering it later during the batch download.
        userbot_enabled = bool(bot_config_repo) and await bot_config_repo.is_userbot_enabled()
        size_ceiling = None if limits.unlimited else (
            settings.MAX_FILE_SIZE_USERBOT if userbot_enabled else settings.MAX_FILE_SIZE
        )
        if size_ceiling is not None and doc.file_size and doc.file_size > size_ceiling:
            await message.answer(f"File too large (max {size_ceiling // (1024 * 1024)} MB).")
            return

        if doc.file_unique_id:
            batch.seen_file_unique_ids.add(doc.file_unique_id)
        batch.pending.append((message, size_ceiling))
        logger.info(f"Merge: buffered file #{len(batch.pending)} of current burst for user {user_id} ('{doc.file_name}')")

        # Requirement: immediate feedback, but only once per burst, and
        # never a partial/running-count queue. The very first file of a
        # fresh burst (no batch task currently active) sends the single
        # "Receiving..." message; every subsequent file in the same burst
        # leaves it untouched.
        if batch.task is None or batch.task.done():
            await _start_new_queue_message(message.bot, state, chat_id, _render_receiving_text())

        previous_task = batch.task
        if previous_task and not previous_task.done():
            previous_task.cancel()
        batch.task = asyncio.create_task(
            _finalize_merge_batch(message.bot, state, chat_id, bot_config_repo)
        )


@router.message(PDFStates.waiting_for_files_merge)
async def pdf_merge_reject_wrong_input(message: Message):
    """Everything that isn't a document reaches here (photos, videos,
    voice, audio, plain text, GIFs, stickers, etc.). Per spec: the user's
    invalid message is deleted, and the validation reply auto-expires
    after ~7s -- never left cluttering the chat.
    """
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Merge: could not delete user's invalid upload message: {e}")
    await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please send PDF files only.")


async def _enter_arrange_screen(bot, state: FSMContext, preserve_order: bool) -> None:
    """Shared by "Done" (fresh order) and "Add More -> Done" (existing
    order preserved, new files appended at the end).
    """
    data = await state.get_data()
    names: List[str] = list(data.get("merge_file_names", []))
    count = len(names)

    order: Optional[List[int]] = data.get("merge_order")
    if not preserve_order or not order:
        order = list(range(count))
    else:
        # Append any newly-added files (Add More) to the end, keep the
        # rest of the previously-confirmed arrangement untouched.
        known = set(order)
        order = order + [i for i in range(count) if i not in known]

    await state.update_data(merge_order=order, merge_add_more_mode=False)
    await state.set_state(PDFStates.waiting_for_merge_arrange)

    sizes: List[Optional[int]] = list(data.get("merge_file_sizes", []))
    await _edit_merge_status(
        bot, state,
        _render_arrange_text(names, sizes, order),
        keyboard=_merge_arrange_keyboard(),
        force=True,
    )


async def _enter_preview_screen(bot, state: FSMContext, order: List[int]) -> None:
    data = await state.get_data()
    names: List[str] = list(data.get("merge_file_names", []))
    sizes: List[Optional[int]] = list(data.get("merge_file_sizes", []))
    await state.update_data(merge_order=order)
    await state.set_state(PDFStates.waiting_for_merge_preview)
    await _edit_merge_status(
        bot, state,
        _render_preview_text(names, sizes, order),
        keyboard=_merge_preview_keyboard(),
        force=True,
    )


@router.callback_query(PDFStates.waiting_for_files_merge, F.data == PDF_DONE)
async def pdf_merge_done(query: CallbackQuery, state: FSMContext, bot_config_repo=None):
    chat_id = query.message.chat.id
    await query.answer()
    # Shares the lock with pdf_merge_receive/_finalize_merge_batch: if a
    # burst is still being collected or is mid-download when Done is
    # pressed, this waits its turn, then immediately drains and downloads
    # whatever is still buffered (rather than just waiting for the natural
    # debounce) before reading the final count -- so a straggling upload
    # is always counted in, never silently dropped.
    async with get_merge_lock(chat_id):
        batch = _merge_batches.get(chat_id)
        failed: List[str] = []
        if batch is not None:
            if batch.task and not batch.task.done():
                batch.task.cancel()
            added, failed = await _process_pending_batch(state, chat_id, bot_config_repo)
            if added or failed:
                logger.info(f"Merge: drained {len(added)} buffered file(s) ({len(failed)} failed) on Done for user {query.from_user.id}")
        cancel_pending_merge_batch(chat_id)

        files = await get_tracked_files(state)
        if len(files) < 2:
            msg = "Need at least 2 PDF files to merge. Send more files, or press Cancel."
            if failed:
                msg += f"\n\n⚠️ {len(failed)} file(s) failed to process and were skipped."
            await query.message.answer(msg)
            return

        data = await state.get_data()
        preserve_order = bool(data.get("merge_order"))
        await _enter_arrange_screen(query.bot, state, preserve_order=preserve_order)
        logger.info(f"Merge: {len(files)} files ready, awaiting order from user {query.from_user.id}")


# --------------------------------------------------------------------------
# Arrange screen
# --------------------------------------------------------------------------

@router.message(PDFStates.waiting_for_merge_arrange, F.text)
async def pdf_merge_arrange_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    names: List[str] = list(data.get("merge_file_names", []))
    current_order: List[int] = list(data.get("merge_order") or range(len(names)))

    new_positions, error = _parse_merge_order(message.text, len(current_order))
    if error:
        # Invalid input -> delete the user's message (kept messages are
        # temporary clutter per spec), show a temporary validation error,
        # and ask again. Queue is left untouched.
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"Merge: could not delete user's invalid order message: {e}")
        await _send_temp_validation_error(message.bot, message.chat.id, f"⚠️ {error}")
        return

    # `new_positions` are 0-based positions *within the currently displayed
    # order*; remap through it so this composes correctly across repeated
    # rearranges and across Add More appends.
    new_order = [current_order[p] for p in new_positions]

    # Keep the conversation clean: the order message did its job, remove it.
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Merge: could not delete user's order message: {e}")

    await _enter_preview_screen(message.bot, state, new_order)


@router.message(PDFStates.waiting_for_merge_arrange)
async def pdf_merge_arrange_wrong_input(message: Message):
    """Catches non-text input (photo, sticker, etc.) while waiting for the
    order string. Same treatment as every other invalid input in Merge:
    delete the user's message, show a temporary validation error.
    """
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Merge: could not delete user's invalid (non-text) order message: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id,
        "⚠️ Please send the new order as numbers separated by commas, e.g. 3,1,2.",
    )


@router.callback_query(PDFStates.waiting_for_merge_arrange, F.data == MERGE_CB_PROCEED_CURRENT_ORDER)
async def pdf_merge_arrange_proceed_current(query: CallbackQuery, state: FSMContext):
    """Skip rearranging entirely and go straight to Preview with the
    order already on screen.
    """
    await query.answer()
    data = await state.get_data()
    names: List[str] = list(data.get("merge_file_names", []))
    order: List[int] = list(data.get("merge_order") or range(len(names)))
    await _enter_preview_screen(query.bot, state, order)


@router.callback_query(PDFStates.waiting_for_merge_arrange, F.data == MERGE_CB_ADD_MORE)
async def pdf_merge_arrange_add_more(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    sizes: List[Optional[int]] = list(data.get("merge_file_sizes", []))
    await state.update_data(merge_add_more_mode=True)
    await state.set_state(PDFStates.waiting_for_files_merge)
    await _edit_merge_status(
        query.bot, state,
        _render_queue_updated_text(len(sizes), sizes, add_more=True),
        keyboard=_merge_upload_keyboard(),
        force=True,
    )


# --------------------------------------------------------------------------
# Preview screen
# --------------------------------------------------------------------------

@router.callback_query(PDFStates.waiting_for_merge_preview, F.data == MERGE_CB_REARRANGE_AGAIN)
async def pdf_merge_preview_rearrange_again(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    names: List[str] = list(data.get("merge_file_names", []))
    sizes: List[Optional[int]] = list(data.get("merge_file_sizes", []))
    order: List[int] = list(data.get("merge_order") or range(len(names)))
    await state.set_state(PDFStates.waiting_for_merge_arrange)
    await _edit_merge_status(
        query.bot, state,
        _render_arrange_text(names, sizes, order),
        keyboard=_merge_arrange_keyboard(),
        force=True,
    )


@router.callback_query(PDFStates.waiting_for_merge_preview, F.data == MERGE_CB_ADD_MORE)
async def pdf_merge_preview_add_more(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    sizes: List[Optional[int]] = list(data.get("merge_file_sizes", []))
    await state.update_data(merge_add_more_mode=True)
    await state.set_state(PDFStates.waiting_for_files_merge)
    await _edit_merge_status(
        query.bot, state,
        _render_queue_updated_text(len(sizes), sizes, add_more=True),
        keyboard=_merge_upload_keyboard(),
        force=True,
    )


@router.callback_query(PDFStates.waiting_for_merge_preview, F.data == MERGE_CB_CONFIRM)
async def pdf_merge_preview_confirm(query: CallbackQuery, state: FSMContext):
    """Only now do we ask for the output filename. This is a brand-new
    message (not an edit of the Preview message) -- the old Preview
    message is deleted first via _start_new_queue_message, so the
    filename prompt is always the newest message in the chat.
    """
    await query.answer()
    chat_id = query.message.chat.id
    files = await get_tracked_files(state)
    await state.set_state(PDFStates.waiting_for_merge_filename)
    text = f"📄 Files Added: {len(files)}\n\n📝 Send the output filename (e.g. physics_notes) -- I'll add .pdf for you."
    await _start_new_queue_message(
        query.bot, state, chat_id, text,
        keyboard=_merge_filename_keyboard(),
    )
    logger.info(f"Merge: order confirmed, awaiting output filename from user {query.from_user.id}")


# --------------------------------------------------------------------------
# Add More -> Done re-enters Arrange, preserving the confirmed order
# (handled by pdf_merge_done / _enter_arrange_screen above, since Add More
# switches state back to waiting_for_files_merge and reuses the same Done
# handler).
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Filename screen (unchanged filename validation/sanitization)
# --------------------------------------------------------------------------

@router.message(PDFStates.waiting_for_merge_filename, F.text)
async def pdf_merge_filename_receive(message: Message, state: FSMContext, user_repo=None, db_user=None, bot_config_repo=None):
    chat_id = message.chat.id
    # Held for the whole operation, including the merge, send, and cleanup
    # below. Also defensively drains any buffered files first (belt and
    # suspenders -- Done should already have done this, but this makes the
    # guarantee "no PDF is ever lost" hold even if that ever changes).
    async with get_merge_lock(chat_id):
        batch = _merge_batches.get(chat_id)
        if batch is not None and batch.pending:
            if batch.task and not batch.task.done():
                batch.task.cancel()
            await _process_pending_batch(state, chat_id, bot_config_repo)
        cancel_pending_merge_batch(chat_id)

        files = await get_tracked_files(state)
        if len(files) < 2:
            await message.answer("Session expired, please start over.")
            await state.clear()
            return

        # Merge in the user-confirmed order, not upload order.
        data = await state.get_data()
        order: Optional[List[int]] = data.get("merge_order")
        if order and len(order) == len(files):
            files = [files[i] for i in order]

        # Unchanged: sanitize the user-supplied name and always end in .pdf.
        safe = sanitize_filename(message.text.strip())
        if safe.lower().endswith(".pdf"):
            safe = safe[:-4]
        if not safe:
            safe = "merged"
        filename = f"{safe}.pdf"

        # Filename accepted -- keep the chat clean, same as the order
        # message on the Arrange screen.
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"Merge: could not delete user's filename message: {e}")

        await _edit_merge_status(
            message.bot, state,
            f"{_QUEUE_DIVIDER}\n📄 Merging PDFs...\n\n⏳ Please wait...\n{_QUEUE_DIVIDER}",
            keyboard=None,
            force=True,
        )
        logger.info(f"Merge: merging {len(files)} files for user {message.from_user.id} -> {filename}")
        try:
            output_path = await PDFMerger().merge(files)
        except Exception as e:
            logger.error(f"Merge: merge failed for user {message.from_user.id}: {e}")
            await _fail(message, state, e, files)
            return

        await track_temp_file(state, output_path)

        userbot_enabled = bool(bot_config_repo) and await bot_config_repo.is_userbot_enabled()
        transport = await get_transport(os.path.getsize(output_path), userbot_enabled)

        output_size = os.path.getsize(output_path)
        cleanup_paths = files + [output_path]
        try:
            await transport.send_document(message, output_path, filename, caption="Here's your merged PDF.")
            await _edit_merge_status(
                message.bot, state,
                f"{_QUEUE_DIVIDER}\n"
                "✅ Merge Successful\n\n"
                f"📄 PDFs Merged: {len(files)}\n"
                f"💾 Output Size: {_format_size(output_size)}\n"
                f"{_QUEUE_DIVIDER}\n\n"
                "Your merged PDF is ready.",
                keyboard=None,
                force=True,
            )
        except Exception:
            logger.exception(f"Merge: failed to send merged output to user {message.from_user.id}")
            raise
        finally:
            delete_paths(cleanup_paths)
            await untrack_temp_files(state, cleanup_paths)
            await state.clear()
            logger.info(f"Merge: cleanup done for user {message.from_user.id} ({len(cleanup_paths)} temp paths)")

        await _track_usage(user_repo, db_user)
        logger.info(f"Merge: sent {filename} to user {message.from_user.id}")


@router.message(PDFStates.waiting_for_merge_filename)
async def pdf_merge_filename_wrong_input(message: Message):
    """Catches non-text input (photo, sticker, etc.) while waiting for the
    output filename. Same treatment as every other invalid input in
    Merge: delete the user's message, show a temporary validation error.
    """
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Merge: could not delete user's invalid (non-text) filename message: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id,
        "⚠️ Please send the output filename as text (e.g. physics_notes).",
    )


# --------------------------------------------------------------------------
# Cancel (always confirmed) / Home -- scoped to the Merge flow's own states
# so these never affect any other PDF tool's Back/Home/Cancel handling.
# --------------------------------------------------------------------------

async def _merge_full_cleanup(state: FSMContext, chat_id: int) -> None:
    cancel_pending_merge_batch(chat_id)
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


@router.callback_query(StateFilter(*_MERGE_STATES), F.data == MERGE_CB_CANCEL)
async def pdf_merge_cancel_ask(query: CallbackQuery, state: FSMContext):
    """Context-aware Cancel: with nothing uploaded yet there's nothing to
    lose, so Cancel returns to the PDF Toolkit menu immediately. Once at
    least one PDF has been uploaded, Cancel always confirms first (on
    every Merge screen), remembering exactly which screen we were on so
    "No, Continue" can restore it losslessly.
    """
    await query.answer()
    chat_id = query.message.chat.id
    current_state = await state.get_state()

    files = await get_tracked_files(state)
    if current_state == PDFStates.waiting_for_files_merge.state and not files:
        await _merge_full_cleanup(state, chat_id)
        await query.message.edit_text(
            "📄 PDF Toolkit -- choose an operation:",
            reply_markup=get_pdf_menu(),
        )
        logger.info(f"Merge: cancelled with empty queue (no confirmation) by user {query.from_user.id}")
        return

    data = await state.get_data()
    await state.update_data(
        merge_pre_cancel_state=current_state,
        merge_pre_cancel_add_more=bool(data.get("merge_add_more_mode")),
    )
    await _edit_merge_status(
        query.bot, state,
        "⚠️ Cancel Merge?\n\n"
        "Are you sure you want to cancel this merge operation?\n"
        "All uploaded PDFs will be removed.",
        keyboard=_merge_cancel_confirm_keyboard(),
        force=True,
    )


@router.callback_query(F.data == MERGE_CB_CANCEL_YES)
async def pdf_merge_cancel_yes(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    await query.answer()
    await _merge_full_cleanup(state, chat_id)
    await query.message.edit_text("❌ Merge cancelled.")
    logger.info(f"Merge: cancelled by user {query.from_user.id}")


@router.callback_query(F.data == MERGE_CB_CANCEL_NO)
async def pdf_merge_cancel_no(query: CallbackQuery, state: FSMContext):
    """Restores exactly the screen the user was on before pressing Cancel
    -- nothing is lost.
    """
    await query.answer()
    data = await state.get_data()
    prev_state = data.get("merge_pre_cancel_state")
    add_more = bool(data.get("merge_pre_cancel_add_more"))
    names: List[str] = list(data.get("merge_file_names", []))
    sizes: List[Optional[int]] = list(data.get("merge_file_sizes", []))
    order: List[int] = list(data.get("merge_order") or range(len(names)))

    await state.set_state(prev_state)

    if prev_state == PDFStates.waiting_for_merge_arrange.state:
        await _edit_merge_status(
            query.bot, state, _render_arrange_text(names, sizes, order),
            keyboard=_merge_arrange_keyboard(), force=True,
        )
    elif prev_state == PDFStates.waiting_for_merge_preview.state:
        await _edit_merge_status(
            query.bot, state, _render_preview_text(names, sizes, order),
            keyboard=_merge_preview_keyboard(), force=True,
        )
    elif prev_state == PDFStates.waiting_for_merge_filename.state:
        files = await get_tracked_files(state)
        await _edit_merge_status(
            query.bot, state,
            f"📄 Files Added: {len(files)}\n\n📝 Send the output filename (e.g. physics_notes) -- I'll add .pdf for you.",
            keyboard=_merge_filename_keyboard(), force=True,
        )
    else:  # waiting_for_files_merge, either fresh upload or Add More
        await _edit_merge_status(
            query.bot, state,
            _render_queue_updated_text(len(sizes), sizes, add_more=add_more),
            keyboard=_merge_upload_keyboard(), force=True,
        )


# --------------------------------------------------------------------------
# Stale-button safety net -- registered last among the Merge callback
# handlers above, so it only ever fires when none of the state-scoped ones
# matched. This happens when /start (or Cancel/Home from another flow) has
# cleared the FSM state but the old Merge message with its inline keyboard
# is still visible on screen: pressing one of those old buttons no longer
# matches any PDFStates-filtered handler, so without this Telegram would
# just spin on "loading" forever with no reply. base.py's
# _reset_to_main_menu already strips the keyboard on reset as the primary
# fix; this is a backstop for anything still reachable (e.g. a button
# pressed in the brief window before the edit lands).
# --------------------------------------------------------------------------

_MERGE_ALL_CALLBACKS = {
    PDF_DONE,
    MERGE_CB_CANCEL,
    MERGE_CB_CANCEL_YES,
    MERGE_CB_CANCEL_NO,
    MERGE_CB_PROCEED_CURRENT_ORDER,
    MERGE_CB_CONFIRM,
    MERGE_CB_REARRANGE_AGAIN,
    MERGE_CB_ADD_MORE,
}


@router.callback_query(StateFilter(None), F.data.in_(_MERGE_ALL_CALLBACKS))
async def pdf_merge_stale_callback(query: CallbackQuery):
    await query.answer("This session has expired. Please start again from the menu.", show_alert=True)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logger.debug(f"Merge: could not strip keyboard from stale callback message: {e}")


# --------------------------------------------------------------------------
# Split (redesigned to match Merge's UX philosophy: clean chat, minimal
# messages, Telegram-native, context-aware Cancel, /start resets everything.
# Reuses PDFSplitter/PDFExtractor and the shared helpers above -- only the
# conversational flow around them is new.)
# --------------------------------------------------------------------------

SPLIT_CB_METHOD_RANGE = "pdfsplit:method_range"
SPLIT_CB_METHOD_EXTRACT = "pdfsplit:method_extract"
SPLIT_CB_METHOD_EVERY = "pdfsplit:method_every"
SPLIT_CB_BACK_TO_METHOD = "pdfsplit:back_to_method"
SPLIT_CB_CANCEL = "pdfsplit:cancel"
SPLIT_CB_CANCEL_YES = "pdfsplit:cancel_yes"
SPLIT_CB_CANCEL_NO = "pdfsplit:cancel_no"
SPLIT_CB_CONFIRM_RANGE = "pdfsplit:confirm_range"
SPLIT_CB_CHANGE_RANGE = "pdfsplit:change_range"
SPLIT_CB_CONFIRM_EXTRACT = "pdfsplit:confirm_extract"
SPLIT_CB_CHANGE_PAGES = "pdfsplit:change_pages"
SPLIT_CB_CONFIRM_EVERY = "pdfsplit:confirm_every"
SPLIT_CB_LARGE_CONTINUE = "pdfsplit:large_continue"

_SPLIT_STATES = (
    PDFStates.waiting_for_file_split,
    PDFStates.waiting_for_split_method,
    PDFStates.waiting_for_split_range_input,
    PDFStates.waiting_for_split_range_preview,
    PDFStates.waiting_for_split_extract_input,
    PDFStates.waiting_for_split_extract_preview,
    PDFStates.waiting_for_split_every_preview,
    PDFStates.waiting_for_split_large_confirm,
)

_SPLIT_LARGE_OUTPUT_THRESHOLD = 20
_SPLIT_UPLOAD_DELAY_SECONDS = 0.8
_SPLIT_PROGRESS_EDIT_INTERVAL_SECONDS = 2.0

# Buffers for the rare case a PDF arrives as part of a Telegram media group
# (album) -- Split only ever accepts ONE PDF, so a whole album must be
# rejected as a unit rather than silently taking the first file. Mirrors
# Merge's buffer-then-debounce approach (see _MergeBatch above) but far
# lighter, since there's nothing to actually queue -- just "is this one
# file, or was it several?".
_split_pending_groups: Dict[str, List[Message]] = {}
_split_group_tasks: Dict[str, asyncio.Task] = {}


def _split_upload_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=SPLIT_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _split_method_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📑 Page Range", callback_data=SPLIT_CB_METHOD_RANGE)
    b.button(text="📄 Extract Specific Pages", callback_data=SPLIT_CB_METHOD_EXTRACT)
    b.button(text="📚 Split Every Page", callback_data=SPLIT_CB_METHOD_EVERY)
    b.button(text="❌ Cancel", callback_data=SPLIT_CB_CANCEL)
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def _split_back_cancel_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=SPLIT_CB_BACK_TO_METHOD)
    b.button(text="❌ Cancel", callback_data=SPLIT_CB_CANCEL)
    b.adjust(2)
    return b.as_markup()


def _split_every_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=SPLIT_CB_BACK_TO_METHOD)
    b.button(text="✅ Split", callback_data=SPLIT_CB_CONFIRM_EVERY)
    b.button(text="❌ Cancel", callback_data=SPLIT_CB_CANCEL)
    b.adjust(2, 1)
    return b.as_markup()


def _split_range_preview_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Split", callback_data=SPLIT_CB_CONFIRM_RANGE)
    b.button(text="🔄 Change Range", callback_data=SPLIT_CB_CHANGE_RANGE)
    b.button(text="❌ Cancel", callback_data=SPLIT_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


def _split_extract_preview_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Extract", callback_data=SPLIT_CB_CONFIRM_EXTRACT)
    b.button(text="🔄 Change Pages", callback_data=SPLIT_CB_CHANGE_PAGES)
    b.button(text="❌ Cancel", callback_data=SPLIT_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


def _split_cancel_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yes, Cancel", callback_data=SPLIT_CB_CANCEL_YES)
    b.button(text="❎ Continue", callback_data=SPLIT_CB_CANCEL_NO)
    b.adjust(2)
    return b.as_markup()


def _split_large_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Continue", callback_data=SPLIT_CB_LARGE_CONTINUE)
    b.button(text="❌ Cancel", callback_data=SPLIT_CB_CANCEL)
    b.adjust(2)
    return b.as_markup()


def _format_page_ranges(pages_0indexed: List[int]) -> str:
    """Render a 0-indexed, already-sorted-by-arrival page list as compact
    1-based ranges for display, e.g. [0,1,2,7] -> '1-3, 8'."""
    if not pages_0indexed:
        return ""
    pages = [p + 1 for p in pages_0indexed]
    parts = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        parts.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = p
    parts.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ", ".join(parts)


def _parse_split_range_tokens(spec: str, total_pages: int) -> List[str]:
    """Validate a comma-separated list of page ranges (e.g.
    '1-5,8-12,20-25') for Split's Page-Range mode. Returns the cleaned,
    validated tokens (each becomes one output PDF) in the order given.
    Raises ValueError with a user-facing message on any problem.
    """
    tokens = [t.strip() for t in spec.split(",") if t.strip() != ""]
    if not tokens:
        raise ValueError("Please enter at least one page range, e.g. 1-5,8-12,20-25")

    seen_keys = set()
    for tok in tokens:
        if "-" in tok:
            bounds = tok.split("-")
            if len(bounds) != 2:
                raise ValueError(f"Invalid range '{tok}'. Example: 1-5,8-12,20-25")
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                raise ValueError(f"Invalid range '{tok}'. Example: 1-5,8-12,20-25")
            if start < 1 or end < 1:
                raise ValueError("Page numbers must be 1 or greater.")
            if start > end:
                raise ValueError(f"Invalid range '{tok}': start page can't be greater than end page.")
            if end > total_pages:
                raise ValueError(f"Range '{tok}' is outside the document ({total_pages} pages).")
            key = (start, end)
        else:
            try:
                p = int(tok)
            except ValueError:
                raise ValueError(f"Invalid page number '{tok}'.")
            if p < 1 or p > total_pages:
                raise ValueError(f"Page {p} is outside the document ({total_pages} pages).")
            key = (p, p)
        if key in seen_keys:
            raise ValueError(f"Duplicate range '{tok}'.")
        seen_keys.add(key)
    return tokens


def _parse_split_extract_tokens(spec: str, total_pages: int) -> List[int]:
    """Validate a comma-separated list of individual page numbers (e.g.
    '1,4,8,15') for Split's Extract-Specific-Pages mode. Returns the
    0-indexed page list in the order given. Raises ValueError with a
    user-facing message on any problem.
    """
    tokens = [t.strip() for t in spec.split(",") if t.strip() != ""]
    if not tokens:
        raise ValueError("Please enter at least one page number, e.g. 1,4,8,15")

    pages: List[int] = []
    seen = set()
    for tok in tokens:
        try:
            p = int(tok)
        except ValueError:
            raise ValueError(f"Invalid page number '{tok}'.")
        if p < 1 or p > total_pages:
            raise ValueError(f"Page {p} is outside the document ({total_pages} pages).")
        if p in seen:
            raise ValueError(f"Duplicate page '{p}'.")
        seen.add(p)
        pages.append(p - 1)
    return pages


def _render_pdf_loaded_text(filename: str, page_count: int, size_bytes: Optional[int]) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "📄 PDF Loaded\n\n"
        f"Filename:\n{_display_name(filename)}\n\n"
        f"Pages:\n{page_count}\n\n"
        f"Size:\n{_format_size(size_bytes)}\n\n"
        "Choose how you'd like to split this PDF.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_range_input_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "📑 Split by Page Range\n\n"
        "Enter one or more page ranges.\n\n"
        "Examples\n"
        "1-5\n"
        "8-12\n"
        "20-25\n\n"
        "Multiple ranges\n"
        "1-5,8-12,20-25\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_extract_input_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "📄 Extract Specific Pages\n\n"
        "Enter the page numbers.\n\n"
        "Example\n"
        "1,4,8,15\n\n"
        "The selected pages will be combined into one PDF.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_every_text(filename: str, page_count: int) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "📚 Split Every Page\n\n"
        "Every page will become its own PDF.\n\n"
        f"Document\n{_display_name(filename)}\n\n"
        f"Pages\n{page_count}\n\n"
        f"{page_count} PDF files will be created.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_range_preview_text(filename: str, tokens: List[str]) -> str:
    lines = [_QUEUE_DIVIDER, "📑 Split Preview", "", f"Document\n{_display_name(filename)}", "", "Output", ""]
    for i, tok in enumerate(tokens, start=1):
        lines.append(f"PDF {i}\nPages {tok}")
        lines.append("")
    lines.append(_QUEUE_DIVIDER)
    return "\n".join(lines).rstrip() 


def _render_extract_preview_text(filename: str, pages_0indexed: List[int]) -> str:
    lines = [_QUEUE_DIVIDER, "📄 Extract Preview", "", f"Document\n{_display_name(filename)}", "", "Selected Pages", ""]
    for p in pages_0indexed:
        lines.append(str(p + 1))
    lines += ["", "A new PDF will be created.", _QUEUE_DIVIDER]
    return "\n".join(lines)


def _render_large_output_text(total: int, est_seconds: float) -> str:
    est = _format_duration(est_seconds)
    return (
        f"{_QUEUE_DIVIDER}\n"
        "📦 Large Output\n\n"
        f"This operation created\n{total} PDF files.\n\n"
        "To avoid Telegram rate limits,\n"
        "the files will be uploaded one by one.\n\n"
        f"Estimated upload time\n≈ {est}\n\n"
        "Continue?\n"
        f"{_QUEUE_DIVIDER}"
    )


def _format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs}s" if secs else f"{minutes} minutes"


def _render_upload_progress_text(done: int, total: int, est_remaining: float) -> str:
    filled = int((done / total) * 14) if total else 0
    bar = "█" * filled + "░" * (14 - filled)
    return (
        f"{_QUEUE_DIVIDER}\n"
        "📤 Uploading Split PDFs...\n\n"
        f"{bar}\n\n"
        f"{done} / {total}\n\n"
        f"Estimated remaining\n≈ {_format_duration(est_remaining)}\n"
        f"{_QUEUE_DIVIDER}"
    )


async def _replace_split_message(bot, state: FSMContext, chat_id: int, text: str, keyboard=None) -> None:
    """Delete the previous Split status message (if any) and send a fresh
    one -- same 'never edit, always replace' pattern Merge uses for its
    queue message, so the newest Split screen always sits at the bottom of
    the chat, right after the user's own uploads/inputs.
    """
    data = await state.get_data()
    old_chat_id = data.get("split_status_chat_id")
    old_message_id = data.get("split_status_message_id")
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Split: status delete skipped: {e}")

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await state.update_data(split_status_chat_id=chat_id, split_status_message_id=sent.message_id)


async def _edit_split_message(bot, state: FSMContext, text: str, keyboard=None) -> Optional[int]:
    """Edit the current tracked Split status message in place (used for the
    Processing -> Large-Output-Confirm -> Upload-Progress -> Completion
    chain, so that whole chain stays a single message being updated rather
    than a burst of new ones). Returns the message_id on success.
    """
    data = await state.get_data()
    chat_id = data.get("split_status_chat_id")
    message_id = data.get("split_status_message_id")
    if chat_id is None or message_id is None:
        return None
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
    except Exception as e:
        logger.debug(f"Split: status edit skipped: {e}")
    return message_id


async def _split_full_cleanup(state: FSMContext, chat_id: int) -> None:
    for key in [k for k in _split_pending_groups if k.startswith(f"{chat_id}:")]:
        _split_pending_groups.pop(key, None)
        task = _split_group_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


async def _reject_split_upload(message: Message, reason: str) -> None:
    """Section 2 / 2A: delete the invalid message(s) and show a temporary
    error, staying in Waiting-for-PDF."""
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Split: could not delete invalid upload message: {e}")
    await _send_temp_validation_error(message.bot, message.chat.id, reason)


# --------------------------------------------------------------------------
# Split -- Section 1/2: entry + waiting for PDF
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_SPLIT)
async def pdf_split_start(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    await state.set_state(PDFStates.waiting_for_file_split)
    await query.message.edit_text(
        f"{_QUEUE_DIVIDER}\n"
        "✂️ Split PDF\n\n"
        "Send the PDF you want to split.\n\n"
        "Supported methods:\n"
        "• 📑 Split by Page Range\n"
        "• 📄 Extract Specific Pages\n"
        "• 📚 Split Every Page\n\n"
        "📄 Send one PDF to begin.\n"
        f"{_QUEUE_DIVIDER}",
        reply_markup=_split_upload_keyboard(),
    )
    await state.update_data(
        split_status_chat_id=chat_id,
        split_status_message_id=query.message.message_id,
    )
    await query.answer()


async def _process_single_split_pdf(message: Message, state: FSMContext, db_user=None) -> None:
    """Section 3 / 3A: download + validate the one accepted PDF, and either
    show the PDF-Loaded method-selection screen or a temporary error.
    """
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        # _download_and_validate already replied with a user-facing error
        # and cleaned up; per spec that error should be temporary and the
        # user's PDF message must be kept -- both already true here.
        return

    try:
        reader = open_pdf_reader(path)
        page_count = check_page_count(reader, min_pages=1)
    except PDFProcessingError as e:
        await untrack_temp_files(state, [path])
        delete_paths([path])
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    filename = message.document.file_name or "document.pdf"
    size_bytes = message.document.file_size

    await state.update_data(
        split_input_path=path,
        split_filename=filename,
        split_page_count=page_count,
        split_file_size=size_bytes,
    )
    await state.set_state(PDFStates.waiting_for_split_method)
    # Section 3: do NOT edit/delete the user's PDF message -- send a new
    # message at the bottom instead.
    sent = await message.answer(
        _render_pdf_loaded_text(filename, page_count, size_bytes),
        reply_markup=_split_method_keyboard(),
    )
    await state.update_data(split_status_chat_id=message.chat.id, split_status_message_id=sent.message_id)


async def _finalize_split_media_group(key: str, state: FSMContext, db_user) -> None:
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    group = _split_pending_groups.pop(key, None)
    _split_group_tasks.pop(key, None)
    if not group:
        return
    if await state.get_state() != PDFStates.waiting_for_file_split.state:
        return  # user navigated away while the album was still arriving

    if len(group) > 1:
        for m in group:
            try:
                await m.delete()
            except Exception as e:
                logger.debug(f"Split: could not delete rejected album message: {e}")
        await _send_temp_validation_error(
            group[0].bot, group[0].chat.id,
            "❌ Please send only ONE PDF file.\n\nSplit PDF works with one document at a time.",
        )
        return

    await _process_single_split_pdf(group[0], state, db_user=db_user)


@router.message(PDFStates.waiting_for_file_split, F.document)
async def pdf_split_receive(message: Message, state: FSMContext, db_user=None):
    doc = message.document
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await _reject_split_upload(message, "❌ Please send a PDF file only.")
        return

    if message.media_group_id:
        # Section 2A: an album might still be arriving -- buffer and debounce
        # instead of acting on the first file that lands.
        key = f"{message.chat.id}:{message.media_group_id}"
        group = _split_pending_groups.setdefault(key, [])
        group.append(message)
        old_task = _split_group_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        _split_group_tasks[key] = asyncio.create_task(
            _finalize_split_media_group(key, state, db_user)
        )
        return

    await _process_single_split_pdf(message, state, db_user=db_user)


@router.message(PDFStates.waiting_for_file_split)
async def pdf_split_receive_invalid(message: Message):
    """Section 2: any non-PDF content (text, photo, sticker, gif, video,
    voice, audio, contact, location, poll, or a non-PDF document) is
    rejected the same way -- delete it, show a temporary error, stay put.
    """
    await _reject_split_upload(message, "❌ Please send a PDF file only.")


# --------------------------------------------------------------------------
# Split -- Section 3/4: method selection + per-method input prompts
# --------------------------------------------------------------------------

@router.callback_query(PDFStates.waiting_for_split_method, F.data == SPLIT_CB_METHOD_RANGE)
async def pdf_split_choose_range(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(PDFStates.waiting_for_split_range_input)
    await _replace_split_message(
        query.bot, state, query.message.chat.id,
        _render_range_input_text(), _split_back_cancel_keyboard(),
    )


@router.callback_query(PDFStates.waiting_for_split_method, F.data == SPLIT_CB_METHOD_EXTRACT)
async def pdf_split_choose_extract(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(PDFStates.waiting_for_split_extract_input)
    await _replace_split_message(
        query.bot, state, query.message.chat.id,
        _render_extract_input_text(), _split_back_cancel_keyboard(),
    )


@router.callback_query(PDFStates.waiting_for_split_method, F.data == SPLIT_CB_METHOD_EVERY)
async def pdf_split_choose_every(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    await state.set_state(PDFStates.waiting_for_split_every_preview)
    await _replace_split_message(
        query.bot, state, query.message.chat.id,
        _render_every_text(data.get("split_filename", "document.pdf"), data.get("split_page_count", 0)),
        _split_every_keyboard(),
    )


@router.callback_query(
    StateFilter(
        PDFStates.waiting_for_split_range_input,
        PDFStates.waiting_for_split_extract_input,
        PDFStates.waiting_for_split_range_preview,
        PDFStates.waiting_for_split_extract_preview,
        PDFStates.waiting_for_split_every_preview,
    ),
    F.data == SPLIT_CB_BACK_TO_METHOD,
)
async def pdf_split_back_to_method(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    await state.set_state(PDFStates.waiting_for_split_method)
    await _replace_split_message(
        query.bot, state, query.message.chat.id,
        _render_pdf_loaded_text(
            data.get("split_filename", "document.pdf"),
            data.get("split_page_count", 0),
            data.get("split_file_size"),
        ),
        _split_method_keyboard(),
    )


# --------------------------------------------------------------------------
# Split -- Section 5A/6A: Page-Range input, validation, and preview
# --------------------------------------------------------------------------

@router.message(PDFStates.waiting_for_split_range_input, F.text)
async def pdf_split_range_input(message: Message, state: FSMContext):
    data = await state.get_data()
    total_pages = data.get("split_page_count", 0)

    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Split: could not delete range input message: {e}")

    try:
        tokens = _parse_split_range_tokens(message.text.strip(), total_pages)
    except ValueError as e:
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    await state.update_data(split_range_tokens=tokens)
    await state.set_state(PDFStates.waiting_for_split_range_preview)
    await _replace_split_message(
        message.bot, state, message.chat.id,
        _render_range_preview_text(data.get("split_filename", "document.pdf"), tokens),
        _split_range_preview_keyboard(),
    )


@router.message(PDFStates.waiting_for_split_range_input)
async def pdf_split_range_input_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Split: could not delete non-text range input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id, "❌ Please send the page range(s) as text. Example: 1-5,8-12"
    )


@router.callback_query(PDFStates.waiting_for_split_range_preview, F.data == SPLIT_CB_CHANGE_RANGE)
async def pdf_split_change_range(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(PDFStates.waiting_for_split_range_input)
    await _replace_split_message(
        query.bot, state, query.message.chat.id,
        _render_range_input_text(), _split_back_cancel_keyboard(),
    )


# --------------------------------------------------------------------------
# Split -- Section 5B/6B: Extract-Specific-Pages input, validation, preview
# --------------------------------------------------------------------------

@router.message(PDFStates.waiting_for_split_extract_input, F.text)
async def pdf_split_extract_input(message: Message, state: FSMContext):
    data = await state.get_data()
    total_pages = data.get("split_page_count", 0)

    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Split: could not delete extract input message: {e}")

    try:
        pages = _parse_split_extract_tokens(message.text.strip(), total_pages)
    except ValueError as e:
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    await state.update_data(split_extract_pages=pages)
    await state.set_state(PDFStates.waiting_for_split_extract_preview)
    await _replace_split_message(
        message.bot, state, message.chat.id,
        _render_extract_preview_text(data.get("split_filename", "document.pdf"), pages),
        _split_extract_preview_keyboard(),
    )


@router.message(PDFStates.waiting_for_split_extract_input)
async def pdf_split_extract_input_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Split: could not delete non-text extract input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id, "❌ Please send the page numbers as text. Example: 1,4,8,15"
    )


@router.callback_query(PDFStates.waiting_for_split_extract_preview, F.data == SPLIT_CB_CHANGE_PAGES)
async def pdf_split_change_pages(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(PDFStates.waiting_for_split_extract_input)
    await _replace_split_message(
        query.bot, state, query.message.chat.id,
        _render_extract_input_text(), _split_back_cancel_keyboard(),
    )


# --------------------------------------------------------------------------
# Split -- Section 7-11: Processing, Large-Output confirm, Upload, Completion
# --------------------------------------------------------------------------

async def _run_split_upload(
    query_or_message,
    state: FSMContext,
    chat_id: int,
    outputs: List[str],
    input_path: str,
    filename_fn,
    user_repo=None,
    db_user=None,
) -> None:
    """Shared tail end for all three Split methods once output file(s) exist
    on disk: optionally confirm on large output counts, then upload one by
    one with a single editable progress message, then report completion.
    Always cleans up every temp path -- input and every output -- no matter
    how far it gets.
    """
    bot = query_or_message.bot
    cleanup_paths = [input_path] + outputs
    total = len(outputs)

    try:
        if total > _SPLIT_LARGE_OUTPUT_THRESHOLD:
            est_seconds = total * _SPLIT_UPLOAD_DELAY_SECONDS
            await state.set_state(PDFStates.waiting_for_split_large_confirm)
            await _edit_split_message(
                bot, state,
                _render_large_output_text(total, est_seconds),
                _split_large_confirm_keyboard(),
            )
            await state.update_data(split_pending_upload=True)
            return  # resumes in pdf_split_large_continue

        await _split_do_upload(bot, state, chat_id, outputs, filename_fn, cleanup_paths, user_repo, db_user)
    except Exception:
        logger.exception("Split: unexpected error preparing upload")
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()
        try:
            await bot.send_message(chat_id, "⚠️ Something went wrong. Please try again.")
        except Exception:
            pass


async def _split_do_upload(bot, state, chat_id, outputs, filename_fn, cleanup_paths, user_repo, db_user) -> None:
    total = len(outputs)
    await _edit_split_message(bot, state, _render_upload_progress_text(0, total, total * _SPLIT_UPLOAD_DELAY_SECONDS))

    last_edit = 0.0
    try:
        for i, path in enumerate(outputs, start=1):
            await bot.send_document(chat_id, FSInputFile(path, filename=filename_fn(i)))
            if i < total:
                await asyncio.sleep(_SPLIT_UPLOAD_DELAY_SECONDS)
            now = time.monotonic()
            remaining = (total - i) * _SPLIT_UPLOAD_DELAY_SECONDS
            if now - last_edit >= _SPLIT_PROGRESS_EDIT_INTERVAL_SECONDS or i == total:
                await _edit_split_message(bot, state, _render_upload_progress_text(i, total, remaining))
                last_edit = now
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)

    await _track_usage(user_repo, db_user)
    data = await state.get_data()
    await state.clear()
    old_chat_id = data.get("split_status_chat_id")
    old_message_id = data.get("split_status_message_id")
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Split: could not delete upload-progress message: {e}")
    await bot.send_message(
        chat_id,
        f"{_QUEUE_DIVIDER}\n"
        "✅ Split Complete\n\n"
        f"{total} PDF files have been sent successfully.\n"
        f"{_QUEUE_DIVIDER}",
    )
    logger.info(f"Split: completed for chat {chat_id}, {total} file(s) sent")


@router.callback_query(PDFStates.waiting_for_split_range_preview, F.data == SPLIT_CB_CONFIRM_RANGE)
async def pdf_split_confirm_range(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    path = data.get("split_input_path")
    tokens: List[str] = list(data.get("split_range_tokens", []))
    if not path or not tokens:
        await query.message.answer("Session expired, please start over.")
        await _split_full_cleanup(state, chat_id)
        return

    await _replace_split_message(query.bot, state, chat_id, "⏳ Splitting PDF...\n\nPlease wait...")

    limits = get_effective_limits(query.from_user.id, db_user)
    spec = ";".join(tokens)
    try:
        outputs = await PDFSplitter().split(path, spec, max_groups=limits.pdf_split_limit)
    except Exception as e:
        await _fail(query.message, state, e, [path])
        return

    for out in outputs:
        await track_temp_file(state, out)

    await _run_split_upload(
        query, state, chat_id, outputs, path,
        filename_fn=lambda i: f"split_part_{i}.pdf",
        user_repo=user_repo, db_user=db_user,
    )


@router.callback_query(PDFStates.waiting_for_split_extract_preview, F.data == SPLIT_CB_CONFIRM_EXTRACT)
async def pdf_split_confirm_extract(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    path = data.get("split_input_path")
    pages: List[int] = list(data.get("split_extract_pages", []))
    if not path or not pages:
        await query.message.answer("Session expired, please start over.")
        await _split_full_cleanup(state, chat_id)
        return

    await _replace_split_message(query.bot, state, chat_id, "⏳ Splitting PDF...\n\nPlease wait...")

    spec = ",".join(str(p + 1) for p in pages)
    try:
        output_path = await PDFExtractor().extract(path, spec)
    except Exception as e:
        await _fail(query.message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _run_split_upload(
        query, state, chat_id, [output_path], path,
        filename_fn=lambda i: "extracted.pdf",
        user_repo=user_repo, db_user=db_user,
    )


@router.callback_query(PDFStates.waiting_for_split_every_preview, F.data == SPLIT_CB_CONFIRM_EVERY)
async def pdf_split_confirm_every(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    path = data.get("split_input_path")
    if not path:
        await query.message.answer("Session expired, please start over.")
        await _split_full_cleanup(state, chat_id)
        return

    await _replace_split_message(query.bot, state, chat_id, "⏳ Splitting PDF...\n\nPlease wait...")

    limits = get_effective_limits(query.from_user.id, db_user)
    try:
        outputs = await PDFSplitter().split(path, None, max_groups=limits.pdf_split_limit)
    except Exception as e:
        await _fail(query.message, state, e, [path])
        return

    for out in outputs:
        await track_temp_file(state, out)

    await _run_split_upload(
        query, state, chat_id, outputs, path,
        filename_fn=lambda i: f"page_{i}.pdf",
        user_repo=user_repo, db_user=db_user,
    )


@router.callback_query(PDFStates.waiting_for_split_large_confirm, F.data == SPLIT_CB_LARGE_CONTINUE)
async def pdf_split_large_continue(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    files = await get_tracked_files(state)
    data = await state.get_data()
    input_path = data.get("split_input_path")
    outputs = [f for f in files if f != input_path]
    if not input_path or not outputs:
        await query.message.answer("Session expired, please start over.")
        await _split_full_cleanup(state, chat_id)
        return

    if "split_range_tokens" in data:
        filename_fn = lambda i: f"split_part_{i}.pdf"
    elif "split_extract_pages" in data:
        filename_fn = lambda i: "extracted.pdf"
    else:
        filename_fn = lambda i: f"page_{i}.pdf"

    cleanup_paths = [input_path] + outputs
    await _split_do_upload(query.bot, state, chat_id, outputs, filename_fn, cleanup_paths, user_repo, db_user)


# --------------------------------------------------------------------------
# Split -- Section 12: Cancel (context-aware) / Section 13: /start handled
# generically by base.py's _reset_to_main_menu (tracked temp files + FSM
# clear cover every Split state the same way it covers Merge).
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_SPLIT_STATES), F.data == SPLIT_CB_CANCEL)
async def pdf_split_cancel_ask(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    current_state = await state.get_state()

    if current_state == PDFStates.waiting_for_file_split.state:
        # Nothing uploaded yet -- cancel immediately, no confirmation.
        await _split_full_cleanup(state, chat_id)
        await query.message.edit_text(
            "📄 PDF Toolkit -- choose an operation:",
            reply_markup=get_pdf_menu(),
        )
        return

    if current_state == PDFStates.waiting_for_split_large_confirm.state:
        # No output has been uploaded yet either -- cancel immediately.
        await _split_full_cleanup(state, chat_id)
        await query.message.edit_text("❌ Split cancelled.")
        return

    await state.update_data(split_pre_cancel_state=current_state)
    await _replace_split_message(
        query.bot, state, chat_id,
        "⚠️ Cancel Split?\n\nYour current progress will be lost.\n\nContinue?",
        _split_cancel_confirm_keyboard(),
    )


@router.callback_query(F.data == SPLIT_CB_CANCEL_YES)
async def pdf_split_cancel_yes(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    await _split_full_cleanup(state, chat_id)
    await query.message.edit_text("❌ Split cancelled.")


@router.callback_query(F.data == SPLIT_CB_CANCEL_NO)
async def pdf_split_cancel_no(query: CallbackQuery, state: FSMContext):
    """Restores exactly the Split screen the user was on before Cancel."""
    await query.answer()
    data = await state.get_data()
    prev_state = data.get("split_pre_cancel_state")
    await state.set_state(prev_state)

    filename = data.get("split_filename", "document.pdf")
    page_count = data.get("split_page_count", 0)
    size_bytes = data.get("split_file_size")

    if prev_state == PDFStates.waiting_for_split_method.state:
        await _replace_split_message(
            query.bot, state, query.message.chat.id,
            _render_pdf_loaded_text(filename, page_count, size_bytes), _split_method_keyboard(),
        )
    elif prev_state == PDFStates.waiting_for_split_range_input.state:
        await _replace_split_message(
            query.bot, state, query.message.chat.id,
            _render_range_input_text(), _split_back_cancel_keyboard(),
        )
    elif prev_state == PDFStates.waiting_for_split_range_preview.state:
        tokens = list(data.get("split_range_tokens", []))
        await _replace_split_message(
            query.bot, state, query.message.chat.id,
            _render_range_preview_text(filename, tokens), _split_range_preview_keyboard(),
        )
    elif prev_state == PDFStates.waiting_for_split_extract_input.state:
        await _replace_split_message(
            query.bot, state, query.message.chat.id,
            _render_extract_input_text(), _split_back_cancel_keyboard(),
        )
    elif prev_state == PDFStates.waiting_for_split_extract_preview.state:
        pages = list(data.get("split_extract_pages", []))
        await _replace_split_message(
            query.bot, state, query.message.chat.id,
            _render_extract_preview_text(filename, pages), _split_extract_preview_keyboard(),
        )
    elif prev_state == PDFStates.waiting_for_split_every_preview.state:
        await _replace_split_message(
            query.bot, state, query.message.chat.id,
            _render_every_text(filename, page_count), _split_every_keyboard(),
        )


# --------------------------------------------------------------------------
# Stale-button safety net -- same rationale as Merge's (see above): if
# /start or another flow's Back/Home/Cancel has already cleared the FSM
# state, an old Split inline keyboard still on screen would otherwise spin
# forever with no reply when pressed.
# --------------------------------------------------------------------------

_SPLIT_ALL_CALLBACKS = {
    SPLIT_CB_METHOD_RANGE, SPLIT_CB_METHOD_EXTRACT, SPLIT_CB_METHOD_EVERY,
    SPLIT_CB_BACK_TO_METHOD, SPLIT_CB_CANCEL, SPLIT_CB_CANCEL_YES, SPLIT_CB_CANCEL_NO,
    SPLIT_CB_CONFIRM_RANGE, SPLIT_CB_CHANGE_RANGE, SPLIT_CB_CONFIRM_EXTRACT,
    SPLIT_CB_CHANGE_PAGES, SPLIT_CB_CONFIRM_EVERY, SPLIT_CB_LARGE_CONTINUE,
}


@router.callback_query(StateFilter(None), F.data.in_(_SPLIT_ALL_CALLBACKS))
async def pdf_split_stale_callback(query: CallbackQuery):
    await query.answer("This session has expired. Please start again from the menu.", show_alert=True)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logger.debug(f"Split: could not strip keyboard from stale callback message: {e}")


# --------------------------------------------------------------------------
# Compress (redesigned to match Merge/Split's UX philosophy: PDF first,
# then analyze it, then choose a mode, then preview/confirm, then compress.
# Reuses the shared helpers above -- _download_and_validate, _fail,
# _send_temp_validation_error, track/untrack_temp_files, etc. -- exactly
# like Split does. Only the conversational flow and the compression engine
# itself (services/pdf/compressor.py) are new.)
# --------------------------------------------------------------------------

COMPRESS_CB_METHOD_BEST = "pdfcompress:method_best"
COMPRESS_CB_METHOD_BALANCED = "pdfcompress:method_balanced"
COMPRESS_CB_METHOD_MAXIMUM = "pdfcompress:method_maximum"
COMPRESS_CB_METHOD_TARGET = "pdfcompress:method_target"
COMPRESS_CB_BACK_TO_METHOD = "pdfcompress:back_to_method"
COMPRESS_CB_CANCEL = "pdfcompress:cancel"
COMPRESS_CB_CANCEL_YES = "pdfcompress:cancel_yes"
COMPRESS_CB_CANCEL_NO = "pdfcompress:cancel_no"
COMPRESS_CB_CONFIRM = "pdfcompress:confirm"
COMPRESS_CB_CHANGE_MODE = "pdfcompress:change_mode"
COMPRESS_CB_CONFIRM_TARGET = "pdfcompress:confirm_target"
COMPRESS_CB_CHANGE_SIZE = "pdfcompress:change_size"

_COMPRESS_STATES = (
    PDFStates.waiting_for_file_compress,
    PDFStates.waiting_for_compress_method,
    PDFStates.waiting_for_compress_preview,
    PDFStates.waiting_for_compress_target_input,
    PDFStates.waiting_for_compress_target_preview,
)

_COMPRESS_MODE_LABELS = {
    CompressionMode.BEST_QUALITY: "🟢 Best Quality",
    CompressionMode.BALANCED: "🟡 Balanced",
    CompressionMode.MAXIMUM: "🔴 Maximum Compression",
}
_COMPRESS_MODE_ICON = {
    CompressionMode.BEST_QUALITY: "🟢",
    CompressionMode.BALANCED: "🟡",
    CompressionMode.MAXIMUM: "🔴",
}
_COMPRESS_MODE_BLURB = {
    CompressionMode.BEST_QUALITY: "This preserves the highest possible quality while reducing file size.",
    CompressionMode.BALANCED: "Provides a good balance between quality and file size.",
    CompressionMode.MAXIMUM: "Produces the smallest possible file.",
}

# Media-group buffering for the "one PDF only" rule -- identical rationale
# to Split's (see _split_pending_groups above): an album needs to be
# rejected as a whole, not silently reduced to its first file.
_compress_pending_groups: Dict[str, List[Message]] = {}
_compress_group_tasks: Dict[str, asyncio.Task] = {}


def _compress_upload_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=COMPRESS_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _compress_method_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🟢 Best Quality", callback_data=COMPRESS_CB_METHOD_BEST)
    b.button(text="🟡 Balanced", callback_data=COMPRESS_CB_METHOD_BALANCED)
    b.button(text="🔴 Maximum Compression", callback_data=COMPRESS_CB_METHOD_MAXIMUM)
    b.button(text="🎯 Target File Size", callback_data=COMPRESS_CB_METHOD_TARGET)
    b.button(text="❌ Cancel", callback_data=COMPRESS_CB_CANCEL)
    b.adjust(1, 1, 1, 1, 1)
    return b.as_markup()


def _compress_preview_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Compress", callback_data=COMPRESS_CB_CONFIRM)
    b.button(text="🔄 Change Mode", callback_data=COMPRESS_CB_CHANGE_MODE)
    b.button(text="❌ Cancel", callback_data=COMPRESS_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


def _compress_target_input_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=COMPRESS_CB_BACK_TO_METHOD)
    b.button(text="❌ Cancel", callback_data=COMPRESS_CB_CANCEL)
    b.adjust(2)
    return b.as_markup()


def _compress_target_preview_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Compress", callback_data=COMPRESS_CB_CONFIRM_TARGET)
    b.button(text="🔄 Change Size", callback_data=COMPRESS_CB_CHANGE_SIZE)
    b.button(text="❌ Cancel", callback_data=COMPRESS_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


def _compress_cancel_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yes, Cancel", callback_data=COMPRESS_CB_CANCEL_YES)
    b.button(text="❎ Continue", callback_data=COMPRESS_CB_CANCEL_NO)
    b.adjust(2)
    return b.as_markup()


def _render_compress_loaded_text(filename: str, page_count: int, size_bytes: Optional[int]) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "📄 PDF Loaded\n\n"
        f"Filename\n{_display_name(filename)}\n\n"
        f"Pages\n{page_count}\n\n"
        f"Current Size\n{_format_size(size_bytes)}\n\n"
        "Analyzing PDF...\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_compress_analysis_text(analysis, page_count: int) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "📊 PDF Analysis\n\n"
        f"Type\n{analysis.doc_type}\n\n"
        f"Pages\n{page_count}\n\n"
        f"Images\n{analysis.image_count}\n\n"
        f"Text\n{analysis.text_amount}\n\n"
        f"Recommended\n{_COMPRESS_MODE_LABELS[analysis.recommended]}\n\n"
        "Choose a compression method.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_compress_preview_text(filename: str, size_bytes: Optional[int], mode: CompressionMode) -> str:
    icon = _COMPRESS_MODE_ICON[mode]
    label = _COMPRESS_MODE_LABELS[mode].split(" ", 1)[1]
    return (
        f"{_QUEUE_DIVIDER}\n"
        f"{icon} Compression Preview\n\n"
        f"Document\n{_display_name(filename)}\n\n"
        f"Current Size\n{_format_size(size_bytes)}\n\n"
        f"Mode\n{label}\n\n"
        f"{_COMPRESS_MODE_BLURB[mode]}\n\n"
        "Continue?\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_compress_target_input_text(size_bytes: Optional[int]) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "🎯 Target File Size\n\n"
        f"Current Size\n{_format_size(size_bytes)}\n\n"
        "Enter your desired file size.\n\n"
        "Examples\n"
        "20\n"
        "15.5\n"
        "10\n\n"
        "(Unit: MB)\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_compress_target_preview_text(original_size: Optional[int], target_bytes: int) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "🎯 Compression Preview\n\n"
        f"Original Size\n{_format_size(original_size)}\n\n"
        f"Target Size\n{_format_size(target_bytes)}\n\n"
        "The PDF will be compressed as close as possible to the requested "
        "size while maintaining the best possible quality.\n\n"
        "Continue?\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_compress_complete_text(info: dict) -> str:
    original = info["original_size"]
    compressed = info["compressed_size"]
    saved = max(original - compressed, 0)
    pct = (saved / original * 100) if original else 0

    if saved <= 0 or pct < 1:
        lines = [
            _QUEUE_DIVIDER,
            "ℹ️ Compression Complete",
            "",
            "This PDF is already well optimized.",
            "No significant size reduction was possible.",
        ]
    else:
        lines = [
            _QUEUE_DIVIDER,
            "✅ Compression Complete",
            "",
            f"Original Size\n{_format_size(original)}",
            "",
            f"Compressed Size\n{_format_size(compressed)}",
            "",
            f"Space Saved\n{_format_size(saved)} ({pct:.0f}%)",
        ]

    if "target_size" in info:
        lines += [
            "",
            f"Target Size\n{_format_size(info['target_size'])}",
            "",
            f"Final Size\n{_format_size(compressed)}",
        ]
        if not info.get("target_achieved", True):
            lines += ["", "The exact target couldn't be reached -- this is the closest achievable size."]

    lines.append(_QUEUE_DIVIDER)
    return "\n".join(lines)


def _parse_target_size_mb(text: str, original_size: Optional[int]) -> int:
    """Validates a user-entered target size in MB. Raises ValueError with a
    user-facing message; returns the target size in bytes."""
    try:
        value = float(text.strip())
    except ValueError:
  
