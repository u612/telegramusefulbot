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
# Merge queue: single-process, in-memory debounce state
# --------------------------------------------------------------------------
# Telegram delivers a "media group" (several PDFs sent together) as a rapid
# burst of separate Message updates, not one atomic update. To collapse
# that burst into a single "Added to Queue" edit on the one status message
# (Task 1 + Task 2) instead of editing per file, each new file (re)schedules
# a short-delay finalize task per chat; only the most recently scheduled
# one for a chat ever actually runs. This is plain in-memory state (not FSM
# data) because it holds a live asyncio.Task, and it's safe because the bot
# runs with aiogram's MemoryStorage -- a single process already.
_merge_batch_tasks: Dict[int, asyncio.Task] = {}


def cancel_pending_merge_batch(chat_id: int) -> None:
    """Cancel any pending merge-batch finalize task for `chat_id`. Called
    whenever the merge flow ends outside of its own Done handler (Cancel,
    Back, Home, a fresh /start) so a stale task never edits a message that
    no longer belongs to an active merge queue.
    """
    task = _merge_batch_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


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
# Merge (multi-file upload + Done button)
# --------------------------------------------------------------------------

async def _edit_merge_status(
    bot,
    state: FSMContext,
    text: str,
    keyboard=None,
    force: bool = False,
) -> None:
    """Edit the single tracked queue status message in place. Never sends a
    new message -- that's the whole point (Task 1: "never spam chat with
    many messages").

    Progress edits are throttled to at most once every
    MERGE_PROGRESS_EDIT_INTERVAL_SECONDS unless `force=True` (used for the
    state-changing edits: batch finalized, filename prompt), so a burst of
    files doesn't trip Telegram's flood limits.
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


async def _finalize_merge_batch(bot, state: FSMContext, chat_id: int) -> None:
    """Runs MERGE_BATCH_FINALIZE_DELAY_SECONDS after the most recent file in
    a burst; if no newer file has rescheduled it in the meantime, does the
    "✅ Added to Queue" edit for the whole batch at once.
    """
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    if _merge_batch_tasks.get(chat_id) is not asyncio.current_task():
        return  # a newer file superseded this task while we were waiting
    _merge_batch_tasks.pop(chat_id, None)

    if await state.get_state() != PDFStates.waiting_for_files_merge.state:
        return  # flow was cancelled/finished/navigated away in the meantime

    count = len(await get_tracked_files(state))
    await _edit_merge_status(
        bot, state,
        f"✅ Added to Queue\n\n📄 Files Added: {count}",
        keyboard=merge_queue_keyboard(),
        force=True,
    )


# --------------------------------------------------------------------------
# Merge (single queue status message, multi-file / media-group aware)
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_MERGE)
async def pdf_merge_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_files_merge)
    await query.message.edit_text(
        "📄 Send the PDF files you want to merge, in order (you can send several at once).\n"
        "I'll keep a running queue right here -- press Done when you're finished.",
        reply_markup=merge_queue_keyboard(),
    )
    await state.update_data(
        merge_status_chat_id=query.message.chat.id,
        merge_status_message_id=query.message.message_id,
        merge_last_status_edit_ts=0.0,
    )
    await query.answer()
    logger.info(f"Merge: queue opened for user {query.from_user.id}")


@router.message(PDFStates.waiting_for_files_merge, F.document)
async def pdf_merge_receive(message: Message, state: FSMContext, user_repo=None, bot_config_repo=None, db_user=None):
    doc = message.document
    user_id = message.from_user.id

    # Task 4: only PDF documents are accepted into the merge queue.
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await message.answer("❌ Please send PDF files only.")
        logger.info(f"Merge: rejected non-PDF document '{doc.file_name}' from user {user_id}")
        return

    # Task 3: captions are never read here -- only the attached document.
    limits = get_effective_limits(user_id, db_user)
    current = await get_tracked_files(state)
    if not limits.unlimited and len(current) >= limits.pdf_queue_limit:
        await message.answer(
            f"You've reached your merge queue limit of {limits.pdf_queue_limit} PDFs. "
            f"Press Done to merge, or Cancel."
        )
        return

    userbot_enabled = bool(bot_config_repo) and await bot_config_repo.is_userbot_enabled()
    # The owner is unlimited on file size everywhere, including Merge. For
    # everyone else the ceiling still depends on which transport will be
    # used (the userbot MTProto path supports much larger files than the
    # plain Bot API) -- that's a technical capability, not a limit we
    # impose, so it stays independent of the centralized limits.file_size.
    size_ceiling = None if limits.unlimited else (
        settings.MAX_FILE_SIZE_USERBOT if userbot_enabled else settings.MAX_FILE_SIZE
    )
    if size_ceiling is not None and doc.file_size and doc.file_size > size_ceiling:
        await message.answer(f"File too large (max {size_ceiling // (1024 * 1024)} MB).")
        return

    position = len(current) + 1
    await _edit_merge_status(
        message.bot, state,
        f"⬇ Downloading...\nProcessing {position}",
        keyboard=merge_queue_keyboard(),
    )

    temp_path = new_temp_path(suffix=".pdf")
    await track_temp_file(state, temp_path)

    transport = await get_transport(doc.file_size or 0, userbot_enabled)
    try:
        await transport.download(message, temp_path)
    except Exception as e:
        logger.error(f"Merge: download failed for user {user_id}: {e}")
        await untrack_temp_files(state, [temp_path])
        delete_paths([temp_path])
        await message.answer("Failed to download that file from Telegram. Please try again.")
        return

    error = validate_upload(
        temp_path, doc.file_name or "file.pdf",
        allowed_extensions=SUPPORTED_PDF_EXTS, allowed_mime_types=_PDF_MIME,
        max_size=size_ceiling,
    )
    if error:
        await untrack_temp_files(state, [temp_path])
        delete_paths([temp_path])
        await message.answer(error)
        logger.info(f"Merge: rejected invalid PDF from user {user_id}: {error}")
        return

    total = len(await get_tracked_files(state))
    logger.info(f"Merge: file {total} queued for user {user_id}")

    # Collapse a burst of files (single message with several PDFs, or a
    # media group) into one "Added to Queue" edit instead of one per file.
    chat_id = message.chat.id
    previous_task = _merge_batch_tasks.get(chat_id)
    if previous_task and not previous_task.done():
        previous_task.cancel()
    _merge_batch_tasks[chat_id] = asyncio.create_task(_finalize_merge_batch(message.bot, state, chat_id))


@router.message(PDFStates.waiting_for_files_merge)
async def pdf_merge_reject_wrong_input(message: Message):
    """Task 4: everything that isn't a document reaches here (photos,
    videos, voice, audio, plain text, GIFs, stickers, etc.) -- reply
    politely instead of ever crashing or silently ignoring it.
    """
    await message.answer("❌ Please send PDF files only.")


@router.callback_query(PDFStates.waiting_for_files_merge, F.data == PDF_DONE)
async def pdf_merge_done(query: CallbackQuery, state: FSMContext):
    cancel_pending_merge_batch(query.message.chat.id)
    files = await get_tracked_files(state)
    await query.answer()
    if len(files) < 2:
        await query.message.answer("Need at least 2 PDF files to merge. Send more files, or press Cancel.")
        return

    await state.set_state(PDFStates.waiting_for_merge_filename)
    await _edit_merge_status(
        query.bot, state,
        f"📄 Files Added: {len(files)}\n\n📝 Send the output filename (e.g. physics_notes) -- I'll add .pdf for you.",
        keyboard=back_home_cancel(),
        force=True,
    )
    logger.info(f"Merge: {len(files)} files ready, awaiting output filename from user {query.from_user.id}")


@router.message(PDFStates.waiting_for_merge_filename, F.text)
async def pdf_merge_filename_receive(message: Message, state: FSMContext, user_repo=None, db_user=None, bot_config_repo=None):
    cancel_pending_merge_batch(message.chat.id)
    files = await get_tracked_files(state)
    if len(files) < 2:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    # Task 8: sanitize the user-supplied name and always end in .pdf.
    safe = sanitize_filename(message.text.strip())
    if safe.lower().endswith(".pdf"):
        safe = safe[:-4]
    if not safe:
        safe = "merged"
    filename = f"{safe}.pdf"

    await message.answer("Merging... please wait.")
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

    cleanup_paths = files + [output_path]
    try:
        await transport.send_document(message, output_path, filename, caption="Here's your merged PDF.")
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
