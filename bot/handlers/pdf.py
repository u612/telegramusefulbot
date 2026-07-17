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
    upload_done_keyboard, compression_level_keyboard, rotate_angle_keyboard,
    pdf_to_images_format_keyboard, merge_queue_keyboard,
    PDF_COMPRESS_LOW, PDF_COMPRESS_MEDIUM, PDF_COMPRESS_HIGH,
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
from services.pdf.compressor import PDFCompressor, CompressionLevel
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
MERGE_CB_HOME = "pdfmerge:home"
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
    b.button(text="🏠 Home", callback_data=MERGE_CB_HOME)
    b.button(text="❌ Cancel", callback_data=MERGE_CB_CANCEL)
    b.adjust(1, 2)
    return b.as_markup()


def _merge_preview_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm", callback_data=MERGE_CB_CONFIRM)
    b.button(text="🔄 Rearrange Again", callback_data=MERGE_CB_REARRANGE_AGAIN)
    b.button(text="➕ Add More", callback_data=MERGE_CB_ADD_MORE)
    b.button(text="🏠 Home", callback_data=MERGE_CB_HOME)
    b.button(text="❌ Cancel", callback_data=MERGE_CB_CANCEL)
    b.adjust(1, 1, 1, 2)
    return b.as_markup()


def _merge_filename_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Home", callback_data=MERGE_CB_HOME)
    b.button(text="❌ Cancel", callback_data=MERGE_CB_CANCEL)
    b.adjust(2)
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
    voice, audio, plain text, GIFs, stickers, etc.) -- reply politely
    instead of ever crashing or silently ignoring it.
    """
    await message.answer("❌ Please send PDF files only.")


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
        # Invalid input -> friendly error, queue untouched, ask again.
        # The user's message is deliberately kept (only a *valid* order
        # gets deleted, per spec) so they can see what they typed.
        await message.answer(f"⚠️ {error}")
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
    await message.answer("Please send the new order as numbers separated by commas, e.g. 3,1,2.")


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
    """Only now do we ask for the output filename."""
    await query.answer()
    files = await get_tracked_files(state)
    await state.set_state(PDFStates.waiting_for_merge_filename)
    text = f"📄 Files Added: {len(files)}\n\n📝 Send the output filename (e.g. physics_notes) -- I'll add .pdf for you."
    await _edit_merge_status(
        query.bot, state, text,
        keyboard=_merge_filename_keyboard(),
        force=True,
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
    await message.answer("Please send the output filename as text (e.g. physics_notes).")


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
    """Cancel is never immediate -- always confirm first, on every Merge
    screen. Remembers exactly which screen we were on so "No, Continue"
    can restore it losslessly.
    """
    await query.answer()
    current_state = await state.get_state()
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


@router.callback_query(StateFilter(*_MERGE_STATES), F.data == MERGE_CB_HOME)
async def pdf_merge_home(query: CallbackQuery, state: FSMContext):
    """Home returns to the PDF Toolkit main menu and also clears the
    Merge session (distinct from Cancel, which asks for confirmation
    first and shows a dedicated cancelled message).
    """
    chat_id = query.message.chat.id
    await query.answer()
    await _merge_full_cleanup(state, chat_id)
    await query.message.edit_text(
        "📄 PDF Toolkit -- choose an operation:",
        reply_markup=get_pdf_menu(),
    )
    logger.info(f"Merge: returned to PDF Toolkit menu by user {query.from_user.id}")


# --------------------------------------------------------------------------
# Split
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_SPLIT)
async def pdf_split_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_file_split)
    await query.message.edit_text(
        "Send the PDF file you want to split.",
        reply_markup=back_home_cancel(),
    )
    await query.answer()


@router.message(PDFStates.waiting_for_file_split, F.document)
async def pdf_split_receive(message: Message, state: FSMContext, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return
    try:
        reader = open_pdf_reader(path)
        page_count = check_page_count(reader, min_pages=2)
    except PDFProcessingError as e:
        await _fail(message, state, e, [path])
        return

    await state.update_data(split_input_path=path, split_page_count=page_count)
    await state.set_state(PDFStates.waiting_for_split_ranges)
    await message.answer(
        f"This PDF has {page_count} pages.\n"
        "Send the split groups, separated by ';'. Each group becomes one output file.\n"
        "Example: 1-3;4-6;7  (or send 'all' to split into one file per page)",
        reply_markup=back_home_cancel(),
    )


@router.message(PDFStates.waiting_for_split_ranges, F.text)
async def pdf_split_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("split_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    spec = None if message.text.strip().lower() == "all" else message.text.strip()
    limits = get_effective_limits(message.from_user.id, db_user)
    await message.answer("Splitting... please wait.")
    try:
        outputs = await PDFSplitter().split(path, spec, max_groups=limits.pdf_split_limit)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    for out in outputs:
        await track_temp_file(state, out)

    await _track_usage(user_repo, db_user)
    await _finish_with_documents(
        message, state, outputs,
        filename_fn=lambda i: f"split_part_{i}.pdf",
        cleanup_paths=[path] + outputs,
    )


# --------------------------------------------------------------------------
# Compress (choose level first, then upload)
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_COMPRESS)
async def pdf_compress_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_compress_level)
    await query.message.edit_text(
        "Choose a compression level:",
        reply_markup=compression_level_keyboard(),
    )
    await query.answer()


@router.callback_query(
    PDFStates.waiting_for_compress_level,
    F.data.in_({PDF_COMPRESS_LOW, PDF_COMPRESS_MEDIUM, PDF_COMPRESS_HIGH}),
)
async def pdf_compress_level_chosen(query: CallbackQuery, state: FSMContext):
    level_map = {
        PDF_COMPRESS_LOW: CompressionLevel.LOW,
        PDF_COMPRESS_MEDIUM: CompressionLevel.MEDIUM,
        PDF_COMPRESS_HIGH: CompressionLevel.HIGH,
    }
    await state.update_data(compress_level=level_map[query.data].value)
    await state.set_state(PDFStates.waiting_for_file_compress)
    await query.message.edit_text(
        "Send the PDF file to compress.",
        reply_markup=back_home_cancel(),
    )
    await query.answer()


@router.message(PDFStates.waiting_for_file_compress, F.document)
async def pdf_compress_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return

    data = await state.get_data()
    level = CompressionLevel(data.get("compress_level", CompressionLevel.MEDIUM.value))
    limits = get_effective_limits(message.from_user.id, db_user)

    await message.answer("Compressing... please wait.")
    try:
        output_path = await PDFCompressor().compress(path, level, timeout=limits.subprocess_timeout)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    await _finish_with_document(message, state, output_path, "compressed.pdf", [path, output_path])


# --------------------------------------------------------------------------
# Rotate (choose angle first, then upload)
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_ROTATE)
async def pdf_rotate_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_rotate_angle)
    await query.message.edit_text("Choose a rotation angle:", reply_markup=rotate_angle_keyboard())
    await query.answer()


@router.callback_query(
    PDFStates.waiting_for_rotate_angle,
    F.data.in_({PDF_ROTATE_90, PDF_ROTATE_180, PDF_ROTATE_270}),
)
async def pdf_rotate_angle_chosen(query: CallbackQuery, state: FSMContext):
    angle_map = {PDF_ROTATE_90: 90, PDF_ROTATE_180: 180, PDF_ROTATE_270: 270}
    await state.update_data(rotate_angle=angle_map[query.data])
    await state.set_state(PDFStates.waiting_for_file_rotate)
    await query.message.edit_text("Send the PDF file to rotate.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_rotate, F.document)
async def pdf_rotate_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return

    data = await state.get_data()
    angle = data.get("rotate_angle", 90)

    await message.answer("Rotating... please wait.")
    try:
        output_path = await PDFRotator().rotate(path, angle)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    await _finish_with_document(message, state, output_path, "rotated.pdf", [path, output_path])


# --------------------------------------------------------------------------
# Extract pages
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_EXTRACT)
async def pdf_extract_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_file_extract)
    await query.message.edit_text("Send the PDF file to extract pages from.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_extract, F.document)
async def pdf_extract_receive(message: Message, state: FSMContext, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return
    try:
        reader = open_pdf_reader(path)
        page_count = check_page_count(reader)
    except PDFProcessingError as e:
        await _fail(message, state, e, [path])
        return

    await state.update_data(extract_input_path=path, extract_page_count=page_count)
    await state.set_state(PDFStates.waiting_for_extract_ranges)
    await message.answer(
        f"This PDF has {page_count} pages.\n"
        "Send the pages to extract, e.g. 1-3,5,9",
        reply_markup=back_home_cancel(),
    )


@router.message(PDFStates.waiting_for_extract_ranges, F.text)
async def pdf_extract_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("extract_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    await message.answer("Extracting... please wait.")
    try:
        output_path = await PDFExtractor().extract(path, message.text.strip())
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    await _finish_with_document(message, state, output_path, "extracted.pdf", [path, output_path])


# --------------------------------------------------------------------------
# Rearrange pages
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_REARRANGE)
async def pdf_rearrange_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_file_rearrange)
    await query.message.edit_text("Send the PDF file to reorder.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_rearrange, F.document)
async def pdf_rearrange_receive(message: Message, state: FSMContext, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return
    try:
        reader = open_pdf_reader(path)
        page_count = check_page_count(reader, min_pages=2)
    except PDFProcessingError as e:
        await _fail(message, state, e, [path])
        return

    await state.update_data(rearrange_input_path=path, rearrange_page_count=page_count)
    await state.set_state(PDFStates.waiting_for_rearrange_order)
    await message.answer(
        f"This PDF has {page_count} pages.\n"
        "Send the new page order, e.g. 3,1,2\n"
        "Every page number from 1 to the page count must appear exactly once.",
        reply_markup=back_home_cancel(),
    )


@router.message(PDFStates.waiting_for_rearrange_order, F.text)
async def pdf_rearrange_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("rearrange_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    await message.answer("Reordering... please wait.")
    try:
        output_path = await PDFRearranger().rearrange(path, message.text.strip())
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    await _finish_with_document(message, state, output_path, "rearranged.pdf", [path, output_path])


# --------------------------------------------------------------------------
# Watermark
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_WATERMARK)
async def pdf_watermark_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_file_watermark)
    await query.message.edit_text("Send the PDF file to watermark.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_watermark, F.document)
async def pdf_watermark_receive(message: Message, state: FSMContext, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return

    await state.update_data(watermark_input_path=path)
    await state.set_state(PDFStates.waiting_for_watermark_text)
    await message.answer("Send the watermark text (max 100 characters).", reply_markup=back_home_cancel())


@router.message(PDFStates.waiting_for_watermark_text, F.text)
async def pdf_watermark_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("watermark_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    await message.answer("Applying watermark... please wait.")
    try:
        output_path = await PDFWatermark().add_watermark(path, message.text)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    await _finish_with_document(message, state, output_path, "watermarked.pdf", [path, output_path])


# --------------------------------------------------------------------------
# Add password
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_ADD_PASSWORD)
async def pdf_add_password_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_file_password_add)
    await query.message.edit_text("Send the PDF file to password-protect.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_password_add, F.document)
async def pdf_add_password_receive(message: Message, state: FSMContext, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return

    await state.update_data(password_add_input_path=path)
    await state.set_state(PDFStates.waiting_for_password_add_value)
    await message.answer(
        "Send the password to set on this PDF.\n"
        "Delete this message from the chat after I confirm, for your own privacy.",
        reply_markup=back_home_cancel(),
    )


@router.message(PDFStates.waiting_for_password_add_value, F.text)
async def pdf_add_password_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("password_add_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    await message.answer("Encrypting... please wait.")
    try:
        output_path = await PDFPassword().add_password(path, message.text)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    await _finish_with_document(
        message, state, output_path, "protected.pdf", [path, output_path],
        caption="Your PDF is now password-protected.",
    )


# --------------------------------------------------------------------------
# Remove password
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_REMOVE_PASSWORD)
async def pdf_remove_password_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_file_password_remove)
    await query.message.edit_text("Send the password-protected PDF file.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_password_remove, F.document)
async def pdf_remove_password_receive(message: Message, state: FSMContext, db_user=None):
    # Note: this deliberately does NOT use _download_and_validate's usual
    # MIME/PdfReader-based path, because open_pdf_reader() rejects encrypted
    # PDFs by default -- exactly the files this flow needs to accept. Basic
    # extension/size checks are still applied.
    doc = message.document
    if doc is None:
        await message.answer("Please send a PDF file as a document.")
        return
    limits = get_effective_limits(message.from_user.id, db_user)
    if not limits.unlimited and doc.file_size and doc.file_size > limits.file_size:
        limit_mb = limits.file_size // (1024 * 1024)
        await message.answer(f"File too large (max {limit_mb} MB).")
        return
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await message.answer("Only .pdf files are supported for this action.")
        return

    path = new_temp_path(suffix=".pdf")
    await track_temp_file(state, path)
    await message.bot.download(doc, destination=path)

    await state.update_data(password_remove_input_path=path)
    await state.set_state(PDFStates.waiting_for_password_remove_value)
    await message.answer("Send the current password for this PDF.", reply_markup=back_home_cancel())


@router.message(PDFStates.waiting_for_password_remove_value, F.text)
async def pdf_remove_password_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("password_remove_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    await message.answer("Removing password... please wait.")
    try:
        output_path = await PDFPassword().remove_password(path, message.text)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    await _finish_with_document(message, state, output_path, "unprotected.pdf", [path, output_path])


# --------------------------------------------------------------------------
# Image -> PDF (multi-file upload + Done button)
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_IMAGE_TO_PDF)
@router.message(PDFStates.waiting_for_images_to_pdf, F.photo)
async def pdf_image_to_pdf_receive_photo(message: Message, state: FSMContext, db_user=None):
    limits = get_effective_limits(message.from_user.id, db_user)

    current = await get_tracked_files(state)

    if not limits.unlimited and len(current) >= limits.image_to_pdf_limit:
        await message.answer(
            f"Maximum of {limits.image_to_pdf_limit} images reached. Press 'Done'."
        )
        return

    # Telegram compresses photos sent as "photo"; use the largest size.
    photo = message.photo[-1]

    path = new_temp_path(suffix=".jpg")
    await track_temp_file(state, path)

    await message.bot.download(photo, destination=path)

    count = len(await get_tracked_files(state))

    if limits.unlimited:
        await message.answer(f"Added image #{count}. Send more images or press 'Done'.")
    else:
        await message.answer(
            f"Added image {count}/{limits.image_to_pdf_limit}. Send more or press 'Done'."
        )


@router.message(PDFStates.waiting_for_images_to_pdf, F.document)
async def pdf_image_to_pdf_receive_doc(message: Message, state: FSMContext, db_user=None):
    limits = get_effective_limits(message.from_user.id, db_user)

    current = await get_tracked_files(state)

    if not limits.unlimited and len(current) >= limits.image_to_pdf_limit:
        await message.answer(
            f"Maximum of {limits.image_to_pdf_limit} images reached. Press 'Done'."
        )
        return

    path = await _download_and_validate(
        message,
        state,
        SUPPORTED_IMAGE_EXTS,
        _IMAGE_MIMES,
        "image",
        db_user=db_user,
    )

    if path is None:
        return

    count = len(await get_tracked_files(state))

    if limits.unlimited:
        await message.answer(f"Added image #{count}. Send more images or press 'Done'.")
    else:
        await message.answer(
            f"Added image {count}/{limits.image_to_pdf_limit}. Send more or press 'Done'."
        )

@router.callback_query(PDFStates.waiting_for_images_to_pdf, F.data == PDF_DONE)
async def pdf_image_to_pdf_done(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    images = await get_tracked_files(state)
    await query.answer()
    if not images:
        await query.message.answer("Send at least one image first, or press Cancel.")
        return

    limits = get_effective_limits(query.from_user.id, db_user)
    await query.message.answer("Converting... please wait.")
    try:
        output_path = await ImageToPDF().convert(images, max_images=limits.image_to_pdf_limit)
    except Exception as e:
        await _fail(query.message, state, e, images)
        return

    await _track_usage(user_repo, db_user)
    await _finish_with_document(query.message, state, output_path, "images.pdf", images + [output_path])


# --------------------------------------------------------------------------
# PDF -> Images (choose format first, then upload)
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_PDF_TO_IMAGES)
async def pdf_to_images_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_format_pdf_to_images)
    await query.message.edit_text("Choose an output image format:", reply_markup=pdf_to_images_format_keyboard())
    await query.answer()


@router.callback_query(
    PDFStates.waiting_for_format_pdf_to_images,
    F.data.in_({PDF_TO_IMG_PNG, PDF_TO_IMG_JPEG}),
)
async def pdf_to_images_format_chosen(query: CallbackQuery, state: FSMContext):
    fmt = "png" if query.data == PDF_TO_IMG_PNG else "jpeg"
    await state.update_data(pdf_to_images_format=fmt)
    await state.set_state(PDFStates.waiting_for_file_pdf_to_images)
    await query.message.edit_text("Send the PDF file to convert to images.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_pdf_to_images, F.document)
async def pdf_to_images_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return

    data = await state.get_data()
    fmt = data.get("pdf_to_images_format", "png")

    await message.answer("Converting to images... please wait.")
    try:
        outputs = await PDFToImages().convert(path, fmt)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    for out in outputs:
        await track_temp_file(state, out)

    ext = "jpg" if fmt == "jpeg" else "png"
    await _track_usage(user_repo, db_user)
    await _finish_with_documents(
        message, state, outputs,
        filename_fn=lambda i: f"page_{i}.{ext}",
        cleanup_paths=[path] + outputs,
    )
