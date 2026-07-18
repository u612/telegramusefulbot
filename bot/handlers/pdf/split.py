"""Split PDF: upload -> analyze -> choose method (page range / extract
specific pages / split every page) -> input -> preview -> confirm ->
process/upload. Everything Split-specific lives here.
"""
import asyncio
import time
from typing import Dict, List, Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.states.pdf import PDFStates
from bot.keyboards.pdf import get_pdf_menu, PDF_SPLIT
from core.constants import SUPPORTED_PDF_EXTS, MERGE_BATCH_FINALIZE_DELAY_SECONDS
from core.logger import logger
from utils.limits import get_effective_limits

from services.pdf.splitter import PDFSplitter
from services.pdf.extractor import PDFExtractor
from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count

from utils.tempfiles import track_temp_file, untrack_temp_files, get_tracked_files, delete_paths
from utils.validators import validate_extension

from .common import (
    _PDF_MIME,
    _QUEUE_DIVIDER,
    _download_and_validate,
    _fail,
    _track_usage,
    _format_size,
    _display_name,
    _send_temp_validation_error,
)

router = Router()

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


