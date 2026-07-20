"""Extract Pages: upload -> analyze -> choose pages -> preview -> confirm ->
process/upload. Mirrors Merge/Split/Compress/Rotate's UX philosophy exactly:
clean chat, minimal messages, Telegram-native, context-aware Cancel, /start
resets everything (via the generic tracked-files + state.clear() path in
bot.handlers.base._reset_to_main_menu).

Extract Pages is NOT Split: Split produces multiple PDFs (one per group).
Extract always produces exactly ONE new PDF containing only the selected
pages, in ascending page order.
"""
import asyncio
from typing import Dict, List, Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.pdf import get_pdf_menu, PDF_EXTRACT
from core.constants import SUPPORTED_PDF_EXTS, MERGE_BATCH_FINALIZE_DELAY_SECONDS
from core.logger import logger

from pypdf import PdfWriter

from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count

from utils.tempfiles import track_temp_file, untrack_temp_files, delete_paths, get_tracked_files, new_temp_path
from utils.validators import validate_extension

from utils.session_manager import register_stale_callbacks
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
# Extract Pages (own StatesGroup so this file is fully self-contained and
# nothing outside Extract needs to change; the generic /start reset in
# bot.handlers.base works on tracked temp files + state.clear() regardless
# of which StatesGroup a state belongs to.)
# --------------------------------------------------------------------------


class ExtractStates(StatesGroup):
    waiting_for_file_extract = State()
    waiting_for_extract_pages_input = State()
    waiting_for_extract_preview = State()


EXTRACT_CB_BACK = "pdfextract:back"
EXTRACT_CB_CANCEL = "pdfextract:cancel"
EXTRACT_CB_CANCEL_YES = "pdfextract:cancel_yes"
EXTRACT_CB_CANCEL_NO = "pdfextract:cancel_no"
EXTRACT_CB_CONFIRM = "pdfextract:confirm"

_EXTRACT_STATES = (
    ExtractStates.waiting_for_file_extract,
    ExtractStates.waiting_for_extract_pages_input,
    ExtractStates.waiting_for_extract_preview,
)

# Buffers for the rare case a PDF arrives as part of a Telegram media group
# (album) -- Extract only ever accepts ONE PDF, so a whole album must be
# rejected as a unit. Mirrors Rotate/Split's buffer-then-debounce approach.
_extract_pending_groups: Dict[str, List[Message]] = {}
_extract_group_tasks: Dict[str, "asyncio.Task"] = {}


def _extract_upload_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=EXTRACT_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _extract_pages_input_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=EXTRACT_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _extract_preview_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Extract", callback_data=EXTRACT_CB_CONFIRM)
    b.button(text="🔙 Back", callback_data=EXTRACT_CB_BACK)
    b.button(text="❌ Cancel", callback_data=EXTRACT_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


def _extract_cancel_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yes, Cancel", callback_data=EXTRACT_CB_CANCEL_YES)
    b.button(text="❎ Continue", callback_data=EXTRACT_CB_CANCEL_NO)
    b.adjust(2)
    return b.as_markup()


def _ext_filename(original_filename: str) -> str:
    """physics.pdf -> physics_ext.pdf, report_final.pdf -> report_final_ext.pdf.
    Always preserves the original stem -- never a generic name.
    """
    name = original_filename or "document.pdf"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return f"{name}_ext.pdf"
    return f"{stem}_ext.{ext}" if ext.lower() == "pdf" else f"{name}_ext.pdf"


def _format_page_ranges(pages_0indexed: List[int]) -> str:
    """0-indexed, sorted page list -> compact 1-based ranges for display,
    e.g. [0,2,3,4,8] -> '1,3-5,9'."""
    if not pages_0indexed:
        return ""
    pages = sorted(p + 1 for p in pages_0indexed)
    parts = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        parts.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = p
    parts.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(parts)


def _parse_extract_page_tokens(spec: str, total_pages: int) -> List[int]:
    """Validates a comma-separated list of page numbers/ranges (e.g. '5',
    '5,8', '3-15', '2,5,8-12,20') for Extract Pages. Returns the sorted,
    de-duplicated 0-indexed page list. Raises ValueError with a user-facing
    message on any problem. Explicitly rejects: 0, negative numbers,
    duplicates, empty input, reversed ranges (5-2), non-numeric tokens, and
    out-of-document pages.
    """
    tokens = [t.strip() for t in spec.split(",") if t.strip() != ""]
    if not tokens:
        raise ValueError("Please enter at least one page or range, e.g. 1,5,8-12")

    seen: set = set()
    pages: List[int] = []
    for tok in tokens:
        if "-" in tok:
            bounds = tok.split("-")
            if len(bounds) != 2 or not bounds[0].strip() or not bounds[1].strip():
                raise ValueError(f"Invalid range '{tok}'. Example: 2-6,10,14")
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                raise ValueError(f"Invalid range '{tok}'. Example: 2-6,10,14")
            if start < 1 or end < 1:
                raise ValueError("Page numbers must be 1 or greater.")
            if start > end:
                raise ValueError(f"Invalid range '{tok}': start page can't be greater than end page.")
            if end > total_pages:
                raise ValueError(f"Range '{tok}' is outside the document ({total_pages} pages).")
            for p0 in range(start - 1, end):
                if p0 in seen:
                    raise ValueError(f"Duplicate page {p0 + 1} in selection.")
                seen.add(p0)
                pages.append(p0)
        else:
            try:
                p = int(tok)
            except ValueError:
                raise ValueError(f"Invalid page number '{tok}'.")
            if p < 1 or p > total_pages:
                raise ValueError(f"Page {p} is outside the document ({total_pages} pages).")
            p0 = p - 1
            if p0 in seen:
                raise ValueError(f"Duplicate page {p} in selection.")
            seen.add(p0)
            pages.append(p0)
    return sorted(pages)


def _render_pdf_loaded_text(filename: str, page_count: int, size_bytes: Optional[int]) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "✅ PDF Loaded\n\n"
        f"📄 Filename:\n{_display_name(filename)}\n\n"
        f"Pages: {page_count}\n\n"
        f"Size: {_format_size(size_bytes)}\n\n"
        "Now choose which pages to extract.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_pdf_loaded_and_pages_text(filename: str, page_count: int, size_bytes: Optional[int]) -> str:
    """Combined 'PDF Loaded' + pages-prompt screen shown immediately after
    upload -- avoids the delete-and-resend flicker of showing '✅ PDF
    Loaded' and then instantly replacing it with the pages prompt.
    """
    return (
        f"{_QUEUE_DIVIDER}\n"
        "✅ PDF Loaded\n"
        f"📄 {_display_name(filename)}\n"
        f"{page_count} Pages • {_format_size(size_bytes)}\n\n"
        "Extract pages\n"
        "Examples below 👇🏻:\n"
        "5\n"
        "5,8\n"
        "3-15\n"
        "2,5,8-12,20\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_pages_input_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Extract Pages\n\n"
        "Send page numbers.\n\n"
        "Examples\n"
        "5\n"
        "5,8\n"
        "3-15\n"
        "2,5,8-12,20\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_preview_text(filename: str, pages_0indexed: List[int]) -> str:
    pages_line = _format_page_ranges(pages_0indexed)
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Ready to Extract\n\n"
        f"📄 {_display_name(filename)}\n\n"
        f"Pages\n{pages_line}\n\n"
        f"Pages Selected\n{len(pages_0indexed)}\n\n"
        f"Output\n{_ext_filename(filename)}\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_completion_caption(filename: str, pages_0indexed: List[int]) -> str:
    pages_line = _format_page_ranges(pages_0indexed)
    return (
        f"{_QUEUE_DIVIDER}\n"
        "✅ Extraction Complete\n\n"
        f"Output\n{_ext_filename(filename)}\n\n"
        f"Extracted Pages\n{pages_line}\n\n"
        f"Total Pages\n{len(pages_0indexed)}\n"
        f"{_QUEUE_DIVIDER}"
    )


async def _replace_extract_message(bot, state: FSMContext, chat_id: int, text: str, keyboard=None) -> None:
    """Delete the previous Extract status message (if any) and send a fresh
    one -- same 'never edit, always replace' pattern Rotate/Split use, so
    the newest Extract screen always sits at the bottom of the chat.
    """
    data = await state.get_data()
    old_chat_id = data.get("extract_status_chat_id")
    old_message_id = data.get("extract_status_message_id")
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Extract: status delete skipped: {e}")

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await state.update_data(extract_status_chat_id=chat_id, extract_status_message_id=sent.message_id)


async def _extract_full_cleanup(state: FSMContext, chat_id: int) -> None:
    for key in [k for k in _extract_pending_groups if k.startswith(f"{chat_id}:")]:
        _extract_pending_groups.pop(key, None)
        task = _extract_group_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


async def _reject_extract_upload(message: Message, reason: str) -> None:
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Extract: could not delete invalid upload message: {e}")
    await _send_temp_validation_error(message.bot, message.chat.id, reason)


def _extract_sync(input_path: str, pages_0indexed: List[int]) -> str:
    reader = open_pdf_reader(input_path)
    writer = PdfWriter()
    try:
        for idx in pages_0indexed:
            writer.add_page(reader.pages[idx])

        output_path = new_temp_path(suffix=".pdf")
        with open(output_path, "wb") as f:
            writer.write(f)
        logger.info(f"Extracted {len(pages_0indexed)} page(s) from {input_path} -> {output_path}")
        return output_path
    finally:
        writer.close()


async def _extract_pages(input_path: str, pages_0indexed: List[int]) -> str:
    return await asyncio.to_thread(_extract_sync, input_path, pages_0indexed)


# --------------------------------------------------------------------------
# Extract -- Step 1: entry + waiting for PDF
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_EXTRACT)
async def pdf_extract_start(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    await state.set_state(ExtractStates.waiting_for_file_extract)
    await query.message.edit_text(
        f"{_QUEUE_DIVIDER}\n"
        "📄 Extract Pages\n\n"
        "Send one PDF.\n\n"
        "A new PDF containing only the selected pages will be created.\n\n"
        "Supported:\n"
        "• One PDF only\n\n"
        "Type /start anytime to return home.\n"
        f"{_QUEUE_DIVIDER}",
        reply_markup=_extract_upload_keyboard(),
    )
    await state.update_data(
        extract_status_chat_id=chat_id,
        extract_status_message_id=query.message.message_id,
    )
    await query.answer()


async def _process_single_extract_pdf(message: Message, state: FSMContext, db_user=None) -> None:
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
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
        extract_input_path=path,
        extract_filename=filename,
        extract_page_count=page_count,
        extract_file_size=size_bytes,
    )
    await state.set_state(ExtractStates.waiting_for_extract_pages_input)
    await _replace_extract_message(
        message.bot, state, message.chat.id,
        _render_pdf_loaded_and_pages_text(filename, page_count, size_bytes),
        _extract_pages_input_keyboard(),
    )


async def _finalize_extract_media_group(key: str, state: FSMContext, db_user) -> None:
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    group = _extract_pending_groups.pop(key, None)
    _extract_group_tasks.pop(key, None)
    if not group:
        return
    if await state.get_state() != ExtractStates.waiting_for_file_extract.state:
        return  # user navigated away while the album was still arriving

    if len(group) > 1:
        for m in group:
            try:
                await m.delete()
            except Exception as e:
                logger.debug(f"Extract: could not delete rejected album message: {e}")
        await _send_temp_validation_error(
            group[0].bot, group[0].chat.id,
            "❌ Please send only ONE PDF file.\n\nExtract Pages works with one document at a time.",
        )
        return

    await _process_single_extract_pdf(group[0], state, db_user=db_user)


@router.message(ExtractStates.waiting_for_file_extract, F.document)
async def pdf_extract_receive(message: Message, state: FSMContext, db_user=None):
    doc = message.document
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await _reject_extract_upload(message, "❌ Please send a PDF file only.")
        return

    if message.media_group_id:
        key = f"{message.chat.id}:{message.media_group_id}"
        group = _extract_pending_groups.setdefault(key, [])
        group.append(message)
        old_task = _extract_group_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        _extract_group_tasks[key] = asyncio.create_task(
            _finalize_extract_media_group(key, state, db_user)
        )
        return

    await _process_single_extract_pdf(message, state, db_user=db_user)


@router.message(ExtractStates.waiting_for_file_extract)
async def pdf_extract_receive_invalid(message: Message):
    """Any non-PDF content (text, photo, sticker, gif, video, voice, audio,
    contact, location, poll, or a non-PDF document) is rejected the same
    way -- delete it, show a temporary error, stay put.
    """
    await _reject_extract_upload(message, "❌ Please send a PDF file only.")


# --------------------------------------------------------------------------
# Extract -- Step 2: pages input + validation
# --------------------------------------------------------------------------

@router.message(ExtractStates.waiting_for_extract_pages_input, F.text)
async def pdf_extract_pages_input(message: Message, state: FSMContext):
    data = await state.get_data()
    total_pages = data.get("extract_page_count", 0)

    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Extract: could not delete pages input message: {e}")

    try:
        pages = _parse_extract_page_tokens(message.text.strip(), total_pages)
    except ValueError:
        await _send_temp_validation_error(
            message.bot, message.chat.id,
            "❌ Invalid page selection.\n\nExamples\n5\n3-10\n1,5,8-12",
        )
        return

    await state.update_data(extract_pages=pages)
    await state.set_state(ExtractStates.waiting_for_extract_preview)
    await _replace_extract_message(
        message.bot, state, message.chat.id,
        _render_preview_text(data.get("extract_filename", "document.pdf"), pages),
        _extract_preview_keyboard(),
    )


@router.message(ExtractStates.waiting_for_extract_pages_input)
async def pdf_extract_pages_input_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Extract: could not delete non-text pages input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id,
        "❌ Invalid page selection.\n\nExamples\n5\n3-10\n1,5,8-12",
    )


# --------------------------------------------------------------------------
# Extract -- Back (context-aware: goes to the immediately previous step)
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_EXTRACT_STATES), F.data == EXTRACT_CB_BACK)
async def pdf_extract_back(query: CallbackQuery, state: FSMContext):
    await query.answer()
    current_state = await state.get_state()
    chat_id = query.message.chat.id

    if current_state == ExtractStates.waiting_for_extract_preview.state:
        await state.set_state(ExtractStates.waiting_for_extract_pages_input)
        await _replace_extract_message(
            query.bot, state, chat_id,
            _render_pages_input_text(), _extract_pages_input_keyboard(),
        )


# --------------------------------------------------------------------------
# Extract -- Step 4/5: Confirm -> process -> upload -> completion
# --------------------------------------------------------------------------

@router.callback_query(ExtractStates.waiting_for_extract_preview, F.data == EXTRACT_CB_CONFIRM)
async def pdf_extract_confirm(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    path = data.get("extract_input_path")
    pages = data.get("extract_pages")
    filename = data.get("extract_filename", "document.pdf")

    if not path or not pages:
        await query.message.answer("Session expired, please start over.")
        await _extract_full_cleanup(state, chat_id)
        return

    await _replace_extract_message(query.bot, state, chat_id, "⏳ Extracting pages...\n\nPlease wait...")

    try:
        output_path = await _extract_pages(path, pages)
    except Exception as e:
        await _fail(query.message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)

    cleanup_paths = [path, output_path]
    try:
        await query.bot.send_document(
            chat_id,
            FSInputFile(output_path, filename=_ext_filename(filename)),
            caption=_render_completion_caption(filename, pages),
        )
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)

    status_data = await state.get_data()
    old_chat_id = status_data.get("extract_status_chat_id")
    old_message_id = status_data.get("extract_status_message_id")
    await state.clear()
    if old_chat_id is not None and old_message_id is not None:
        try:
            await query.bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Extract: could not delete processing message: {e}")

    logger.info(f"Extract: completed for chat {chat_id}")


# --------------------------------------------------------------------------
# Extract -- Cancel (context-aware, same pattern as Rotate/Split)
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_EXTRACT_STATES), F.data == EXTRACT_CB_CANCEL)
async def pdf_extract_cancel_ask(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    current_state = await state.get_state()

    if current_state == ExtractStates.waiting_for_file_extract.state:
        # Nothing uploaded yet -- cancel immediately, no confirmation.
        await _extract_full_cleanup(state, chat_id)
        await query.message.edit_text(
            "📄 PDF Toolkit -- choose an operation:",
            reply_markup=get_pdf_menu(),
        )
        return

    await state.update_data(extract_pre_cancel_state=current_state)
    await _replace_extract_message(
        query.bot, state, chat_id,
        "⚠️ Cancel Extract Pages?\n\nYour current progress will be lost.\n\nContinue?",
        _extract_cancel_confirm_keyboard(),
    )


@router.callback_query(F.data == EXTRACT_CB_CANCEL_YES)
async def pdf_extract_cancel_yes(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    await _extract_full_cleanup(state, chat_id)
    await query.message.edit_text("❌ Extract Pages cancelled.")


@router.callback_query(F.data == EXTRACT_CB_CANCEL_NO)
async def pdf_extract_cancel_no(query: CallbackQuery, state: FSMContext):
    """Restores exactly the Extract screen the user was on before Cancel."""
    await query.answer()
    data = await state.get_data()
    prev_state = data.get("extract_pre_cancel_state")
    await state.set_state(prev_state)
    chat_id = query.message.chat.id

    filename = data.get("extract_filename", "document.pdf")
    pages = data.get("extract_pages")

    if prev_state == ExtractStates.waiting_for_extract_pages_input.state:
        await _replace_extract_message(
            query.bot, state, chat_id,
            _render_pages_input_text(), _extract_pages_input_keyboard(),
        )
    elif prev_state == ExtractStates.waiting_for_extract_preview.state:
        await _replace_extract_message(
            query.bot, state, chat_id,
            _render_preview_text(filename, pages or []), _extract_preview_keyboard(),
        )


# --------------------------------------------------------------------------
# Stale-button safety net -- same rationale as Rotate/Split's: if /start or
# another flow's Back/Home/Cancel has already cleared the FSM state, an old
# Extract inline keyboard still on screen would otherwise spin forever with
# no reply when pressed.
# --------------------------------------------------------------------------

register_stale_callbacks(exact={
    EXTRACT_CB_BACK, EXTRACT_CB_CANCEL, EXTRACT_CB_CANCEL_YES, EXTRACT_CB_CANCEL_NO, EXTRACT_CB_CONFIRM,
})
