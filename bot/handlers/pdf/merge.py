"""Merge PDF: buffer -> debounce -> download-in-order -> commit -> arrange
-> preview -> filename -> merge. Everything Merge-specific lives here.
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
from bot.keyboards.pdf import get_pdf_menu, PDF_MERGE, PDF_DONE
from core.constants import (
    SUPPORTED_PDF_EXTS,
    MERGE_PROGRESS_EDIT_INTERVAL_SECONDS, MERGE_BATCH_FINALIZE_DELAY_SECONDS,
)
from core.config import settings
from core.logger import logger
from utils.limits import get_effective_limits
from services.telegram import get_transport

from services.pdf.merger import PDFMerger

from utils.tempfiles import (
    new_temp_path,
    track_temp_file,
    untrack_temp_files,
    get_tracked_files,
    delete_paths,
)
from utils.validators import validate_extension, validate_upload, sanitize_filename

from .common import (
    _PDF_MIME,
    _QUEUE_DIVIDER,
    _DISPLAY_NAME_MAX,
    _track_usage,
    _fail,
    _format_size,
    _display_name,
    _send_temp_validation_error,
)

router = Router()

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


