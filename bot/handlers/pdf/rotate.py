"""Rotate PDF: upload -> analyze -> choose target (entire PDF / selected
pages) -> [page numbers ->] choose rotation -> preview -> confirm ->
process/upload. Mirrors Split/Compress's UX philosophy exactly: clean
chat, minimal messages, Telegram-native, context-aware Cancel, /start
resets everything (via the generic tracked-files + state.clear() path in
bot.handlers.base._reset_to_main_menu).
"""
import asyncio
from typing import Dict, List, Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.pdf import get_pdf_menu, PDF_ROTATE
from core.constants import SUPPORTED_PDF_EXTS, MERGE_BATCH_FINALIZE_DELAY_SECONDS
from core.logger import logger

from services.pdf.rotator import PDFRotator
from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count

from utils.tempfiles import track_temp_file, untrack_temp_files, delete_paths, get_tracked_files
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
# Rotate (redesigned to match Split's UX philosophy -- own StatesGroup so
# this file is fully self-contained and nothing outside Rotate needs to
# change; the generic /start reset in bot.handlers.base works on tracked
# temp files + state.clear() regardless of which StatesGroup a state
# belongs to.)
# --------------------------------------------------------------------------


class RotateStates(StatesGroup):
    waiting_for_file_rotate = State()
    waiting_for_rotate_target = State()
    waiting_for_rotate_pages_input = State()
    waiting_for_rotate_angle_entire = State()
    waiting_for_rotate_angle_selected = State()
    waiting_for_rotate_preview = State()


ROTATE_CB_TARGET_ALL = "pdfrotate:target_all"
ROTATE_CB_TARGET_SELECTED = "pdfrotate:target_selected"
ROTATE_CB_ANGLE_LEFT = "pdfrotate:angle_left"
ROTATE_CB_ANGLE_RIGHT = "pdfrotate:angle_right"
ROTATE_CB_ANGLE_180 = "pdfrotate:angle_180"
ROTATE_CB_BACK = "pdfrotate:back"
ROTATE_CB_CANCEL = "pdfrotate:cancel"
ROTATE_CB_CANCEL_YES = "pdfrotate:cancel_yes"
ROTATE_CB_CANCEL_NO = "pdfrotate:cancel_no"
ROTATE_CB_CONFIRM = "pdfrotate:confirm"

_ROTATE_STATES = (
    RotateStates.waiting_for_file_rotate,
    RotateStates.waiting_for_rotate_target,
    RotateStates.waiting_for_rotate_pages_input,
    RotateStates.waiting_for_rotate_angle_entire,
    RotateStates.waiting_for_rotate_angle_selected,
    RotateStates.waiting_for_rotate_preview,
)

# pypdf's page.rotate(angle) rotates clockwise. "90° Left" (counter
# -clockwise) is therefore clockwise 270; "90° Right" is clockwise 90.
_ANGLE_DEGREES = {
    ROTATE_CB_ANGLE_LEFT: 270,
    ROTATE_CB_ANGLE_RIGHT: 90,
    ROTATE_CB_ANGLE_180: 180,
}
_ANGLE_LABELS = {
    ROTATE_CB_ANGLE_LEFT: "↩️ 90° Counter-clockwise",
    ROTATE_CB_ANGLE_RIGHT: "↪️ 90° Clockwise",
    ROTATE_CB_ANGLE_180: "🔄 180°",
}

# Buffers for the rare case a PDF arrives as part of a Telegram media group
# (album) -- Rotate only ever accepts ONE PDF, so a whole album must be
# rejected as a unit. Mirrors Split's buffer-then-debounce approach.
_rotate_pending_groups: Dict[str, List[Message]] = {}
_rotate_group_tasks: Dict[str, "asyncio.Task"] = {}


def _rotate_upload_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=ROTATE_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _rotate_target_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🌐 Entire PDF", callback_data=ROTATE_CB_TARGET_ALL)
    b.button(text="📄 Selected Pages", callback_data=ROTATE_CB_TARGET_SELECTED)
    b.button(text="❌ Cancel", callback_data=ROTATE_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


def _rotate_pages_input_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Back", callback_data=ROTATE_CB_BACK)
    b.button(text="❌ Cancel", callback_data=ROTATE_CB_CANCEL)
    b.adjust(2)
    return b.as_markup()


def _rotate_angle_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="↩️ 90° Left", callback_data=ROTATE_CB_ANGLE_LEFT)
    b.button(text="↪️ 90° Right", callback_data=ROTATE_CB_ANGLE_RIGHT)
    b.button(text="🔄 180°", callback_data=ROTATE_CB_ANGLE_180)
    b.button(text="🔙 Back", callback_data=ROTATE_CB_BACK)
    b.button(text="❌ Cancel", callback_data=ROTATE_CB_CANCEL)
    b.adjust(1, 1, 1, 1, 1)
    return b.as_markup()


def _rotate_preview_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Rotate", callback_data=ROTATE_CB_CONFIRM)
    b.button(text="🔙 Back", callback_data=ROTATE_CB_BACK)
    b.button(text="❌ Cancel", callback_data=ROTATE_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


def _rotate_cancel_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yes, Cancel", callback_data=ROTATE_CB_CANCEL_YES)
    b.button(text="❎ Continue", callback_data=ROTATE_CB_CANCEL_NO)
    b.adjust(2)
    return b.as_markup()


def _rt_filename(original_filename: str) -> str:
    """physics.pdf -> physics_rt.pdf, report.pdf -> report_rt.pdf,
    notes_final.pdf -> notes_final_rt.pdf. Always preserves the original
    stem -- never a generic name.
    """
    name = original_filename or "document.pdf"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return f"{name}_rt.pdf"
    return f"{stem}_rt.{ext}" if ext.lower() == "pdf" else f"{name}_rt.pdf"


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


def _parse_rotate_page_tokens(spec: str, total_pages: int) -> List[int]:
    """Validates a comma-separated list of page numbers/ranges (e.g.
    '1', '3-8', '1,3,5', '2-6,10,14') for Rotate's Selected-Pages mode.
    Returns the sorted, de-duplicated 0-indexed page list. Raises
    ValueError with a user-facing message on any problem.
    """
    tokens = [t.strip() for t in spec.split(",") if t.strip() != ""]
    if not tokens:
        raise ValueError("Please enter at least one page or range, e.g. 1,3-5,9")

    pages = set()
    for tok in tokens:
        if "-" in tok:
            bounds = tok.split("-")
            if len(bounds) != 2:
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
            pages.update(range(start - 1, end))
        else:
            try:
                p = int(tok)
            except ValueError:
                raise ValueError(f"Invalid page number '{tok}'.")
            if p < 1 or p > total_pages:
                raise ValueError(f"Page {p} is outside the document ({total_pages} pages).")
            pages.add(p - 1)
    return sorted(pages)


def _render_pdf_loaded_text(filename: str, page_count: int, size_bytes: Optional[int]) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "✅ PDF Loaded\n\n"
        f"📄 Filename:\n{_display_name(filename)}\n\n"
        f"Pages: {page_count}\n\n"
        f"Size: {_format_size(size_bytes)}\n\n"
        "Choose what you'd like to rotate.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_pages_input_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Rotate Selected Pages\n\n"
        "Send page numbers.\n\n"
        "Examples\n"
        "1\n"
        "3-8\n"
        "1,3,5\n"
        "2-6,10,14\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_pages_selected_text(pages_0indexed: List[int]) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Pages Selected\n\n"
        f"{_format_page_ranges(pages_0indexed)}\n\n"
        "Choose rotation.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_angle_entire_text() -> str:
    return f"{_QUEUE_DIVIDER}\nChoose rotation for the entire PDF.\n{_QUEUE_DIVIDER}"


def _render_preview_text(filename: str, page_count: int, pages_0indexed: Optional[List[int]], angle_cb: str) -> str:
    pages_line = f"All {page_count}" if pages_0indexed is None else _format_page_ranges(pages_0indexed)
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Ready to Rotate\n\n"
        f"📄 {_display_name(filename)}\n\n"
        f"Pages\n{pages_line}\n\n"
        f"Rotation\n{_ANGLE_LABELS[angle_cb]}\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_completion_text(filename: str, page_count: int, pages_0indexed: Optional[List[int]], angle_cb: str) -> str:
    pages_line = f"All {page_count}" if pages_0indexed is None else _format_page_ranges(pages_0indexed)
    return (
        f"{_QUEUE_DIVIDER}\n"
        "✅ Rotation Complete\n\n"
        f"📄 {_display_name(filename)}\n\n"
        f"Rotation\n{_ANGLE_LABELS[angle_cb]}\n\n"
        f"Pages\n{pages_line}\n"
        f"{_QUEUE_DIVIDER}"
    )


async def _replace_rotate_message(bot, state: FSMContext, chat_id: int, text: str, keyboard=None) -> None:
    """Delete the previous Rotate status message (if any) and send a fresh
    one -- same 'never edit, always replace' pattern Split/Merge use, so
    the newest Rotate screen always sits at the bottom of the chat.
    """
    data = await state.get_data()
    old_chat_id = data.get("rotate_status_chat_id")
    old_message_id = data.get("rotate_status_message_id")
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Rotate: status delete skipped: {e}")

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await state.update_data(rotate_status_chat_id=chat_id, rotate_status_message_id=sent.message_id)


async def _rotate_full_cleanup(state: FSMContext, chat_id: int) -> None:
    for key in [k for k in _rotate_pending_groups if k.startswith(f"{chat_id}:")]:
        _rotate_pending_groups.pop(key, None)
        task = _rotate_group_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


async def _reject_rotate_upload(message: Message, reason: str) -> None:
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rotate: could not delete invalid upload message: {e}")
    await _send_temp_validation_error(message.bot, message.chat.id, reason)


# --------------------------------------------------------------------------
# Rotate -- Step 1: entry + waiting for PDF
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_ROTATE)
async def pdf_rotate_start(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    await state.set_state(RotateStates.waiting_for_file_rotate)
    await query.message.edit_text(
        f"{_QUEUE_DIVIDER}\n"
        "🔄 Rotate PDF\n\n"
        "Send one PDF to rotate.\n\n"
        "Supported:\n"
        "• One PDF only\n\n"
        "Type /start anytime to return home.\n"
        f"{_QUEUE_DIVIDER}",
        reply_markup=_rotate_upload_keyboard(),
    )
    await state.update_data(
        rotate_status_chat_id=chat_id,
        rotate_status_message_id=query.message.message_id,
    )
    await query.answer()


async def _process_single_rotate_pdf(message: Message, state: FSMContext, db_user=None) -> None:
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
        rotate_input_path=path,
        rotate_filename=filename,
        rotate_page_count=page_count,
        rotate_file_size=size_bytes,
    )
    await state.set_state(RotateStates.waiting_for_rotate_target)
    sent = await message.answer(
        _render_pdf_loaded_text(filename, page_count, size_bytes),
        reply_markup=_rotate_target_keyboard(),
    )
    await state.update_data(rotate_status_chat_id=message.chat.id, rotate_status_message_id=sent.message_id)


async def _finalize_rotate_media_group(key: str, state: FSMContext, db_user) -> None:
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    group = _rotate_pending_groups.pop(key, None)
    _rotate_group_tasks.pop(key, None)
    if not group:
        return
    if await state.get_state() != RotateStates.waiting_for_file_rotate.state:
        return  # user navigated away while the album was still arriving

    if len(group) > 1:
        for m in group:
            try:
                await m.delete()
            except Exception as e:
                logger.debug(f"Rotate: could not delete rejected album message: {e}")
        await _send_temp_validation_error(
            group[0].bot, group[0].chat.id,
            "❌ Please send only ONE PDF file.\n\nRotate PDF works with one document at a time.",
        )
        return

    await _process_single_rotate_pdf(group[0], state, db_user=db_user)


@router.message(RotateStates.waiting_for_file_rotate, F.document)
async def pdf_rotate_receive(message: Message, state: FSMContext, db_user=None):
    doc = message.document
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await _reject_rotate_upload(message, "❌ Please send a PDF file only.")
        return

    if message.media_group_id:
        key = f"{message.chat.id}:{message.media_group_id}"
        group = _rotate_pending_groups.setdefault(key, [])
        group.append(message)
        old_task = _rotate_group_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        _rotate_group_tasks[key] = asyncio.create_task(
            _finalize_rotate_media_group(key, state, db_user)
        )
        return

    await _process_single_rotate_pdf(message, state, db_user=db_user)


@router.message(RotateStates.waiting_for_file_rotate)
async def pdf_rotate_receive_invalid(message: Message):
    """Any non-PDF content (text, photo, sticker, gif, video, voice, audio,
    contact, location, poll, or a non-PDF document) is rejected the same
    way -- delete it, show a temporary error, stay put.
    """
    await _reject_rotate_upload(message, "❌ Please send a PDF file only.")


# --------------------------------------------------------------------------
# Rotate -- Step 2: choose target (Entire PDF / Selected Pages)
# --------------------------------------------------------------------------

@router.callback_query(RotateStates.waiting_for_rotate_target, F.data == ROTATE_CB_TARGET_ALL)
async def pdf_rotate_choose_all(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(rotate_pages=None)
    await state.set_state(RotateStates.waiting_for_rotate_angle_entire)
    await _replace_rotate_message(
        query.bot, state, query.message.chat.id,
        _render_angle_entire_text(), _rotate_angle_keyboard(),
    )


@router.callback_query(RotateStates.waiting_for_rotate_target, F.data == ROTATE_CB_TARGET_SELECTED)
async def pdf_rotate_choose_selected(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(RotateStates.waiting_for_rotate_pages_input)
    await _replace_rotate_message(
        query.bot, state, query.message.chat.id,
        _render_pages_input_text(), _rotate_pages_input_keyboard(),
    )


# --------------------------------------------------------------------------
# Rotate -- Step 2A: Selected-Pages input + validation
# --------------------------------------------------------------------------

@router.message(RotateStates.waiting_for_rotate_pages_input, F.text)
async def pdf_rotate_pages_input(message: Message, state: FSMContext):
    data = await state.get_data()
    total_pages = data.get("rotate_page_count", 0)

    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rotate: could not delete pages input message: {e}")

    try:
        pages = _parse_rotate_page_tokens(message.text.strip(), total_pages)
    except ValueError:
        await _send_temp_validation_error(
            message.bot, message.chat.id,
            "❌ Invalid page selection.\n\nExamples\n1\n4-10\n1,3,5-7",
        )
        return

    await state.update_data(rotate_pages=pages)
    await state.set_state(RotateStates.waiting_for_rotate_angle_selected)
    await _replace_rotate_message(
        message.bot, state, message.chat.id,
        _render_pages_selected_text(pages), _rotate_angle_keyboard(),
    )


@router.message(RotateStates.waiting_for_rotate_pages_input)
async def pdf_rotate_pages_input_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rotate: could not delete non-text pages input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id,
        "❌ Invalid page selection.\n\nExamples\n1\n4-10\n1,3,5-7",
    )


# --------------------------------------------------------------------------
# Rotate -- Step 3: rotation angle chosen -> preview
# --------------------------------------------------------------------------

@router.callback_query(
    StateFilter(RotateStates.waiting_for_rotate_angle_entire, RotateStates.waiting_for_rotate_angle_selected),
    F.data.in_({ROTATE_CB_ANGLE_LEFT, ROTATE_CB_ANGLE_RIGHT, ROTATE_CB_ANGLE_180}),
)
async def pdf_rotate_angle_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    await state.update_data(rotate_angle_cb=query.data)
    await state.set_state(RotateStates.waiting_for_rotate_preview)
    await _replace_rotate_message(
        query.bot, state, query.message.chat.id,
        _render_preview_text(
            data.get("rotate_filename", "document.pdf"),
            data.get("rotate_page_count", 0),
            data.get("rotate_pages"),
            query.data,
        ),
        _rotate_preview_keyboard(),
    )


# --------------------------------------------------------------------------
# Rotate -- Back (context-aware: goes to the immediately previous step)
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_ROTATE_STATES), F.data == ROTATE_CB_BACK)
async def pdf_rotate_back(query: CallbackQuery, state: FSMContext):
    await query.answer()
    current_state = await state.get_state()
    data = await state.get_data()
    chat_id = query.message.chat.id

    if current_state in (
        RotateStates.waiting_for_rotate_pages_input.state,
        RotateStates.waiting_for_rotate_angle_entire.state,
    ):
        await state.set_state(RotateStates.waiting_for_rotate_target)
        await _replace_rotate_message(
            query.bot, state, chat_id,
            _render_pdf_loaded_text(
                data.get("rotate_filename", "document.pdf"),
                data.get("rotate_page_count", 0),
                data.get("rotate_file_size"),
            ),
            _rotate_target_keyboard(),
        )
    elif current_state == RotateStates.waiting_for_rotate_angle_selected.state:
        await state.set_state(RotateStates.waiting_for_rotate_pages_input)
        await _replace_rotate_message(
            query.bot, state, chat_id,
            _render_pages_input_text(), _rotate_pages_input_keyboard(),
        )
    elif current_state == RotateStates.waiting_for_rotate_preview.state:
        pages = data.get("rotate_pages")
        if pages is None:
            await state.set_state(RotateStates.waiting_for_rotate_angle_entire)
            await _replace_rotate_message(
                query.bot, state, chat_id,
                _render_angle_entire_text(), _rotate_angle_keyboard(),
            )
        else:
            await state.set_state(RotateStates.waiting_for_rotate_angle_selected)
            await _replace_rotate_message(
                query.bot, state, chat_id,
                _render_pages_selected_text(pages), _rotate_angle_keyboard(),
            )


# --------------------------------------------------------------------------
# Rotate -- Step 4/5: Confirm -> process -> upload -> completion
# --------------------------------------------------------------------------

@router.callback_query(RotateStates.waiting_for_rotate_preview, F.data == ROTATE_CB_CONFIRM)
async def pdf_rotate_confirm(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    path = data.get("rotate_input_path")
    angle_cb = data.get("rotate_angle_cb")
    pages = data.get("rotate_pages")
    filename = data.get("rotate_filename", "document.pdf")
    page_count = data.get("rotate_page_count", 0)

    if not path or angle_cb not in _ANGLE_DEGREES:
        await query.message.answer("Session expired, please start over.")
        await _rotate_full_cleanup(state, chat_id)
        return

    await _replace_rotate_message(query.bot, state, chat_id, "⏳ Rotating PDF...\n\nPlease wait...")

    angle = _ANGLE_DEGREES[angle_cb]
    try:
        output_path = await PDFRotator().rotate(path, angle, pages=pages)
    except Exception as e:
        await _fail(query.message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)

    cleanup_paths = [path, output_path]
    try:
        await query.bot.send_document(
            chat_id, FSInputFile(output_path, filename=_rt_filename(filename)),
        )
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)

    status_data = await state.get_data()
    old_chat_id = status_data.get("rotate_status_chat_id")
    old_message_id = status_data.get("rotate_status_message_id")
    await state.clear()
    if old_chat_id is not None and old_message_id is not None:
        try:
            await query.bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Rotate: could not delete processing message: {e}")

    await query.bot.send_message(
        chat_id,
        _render_completion_text(filename, page_count, pages, angle_cb),
    )
    logger.info(f"Rotate: completed for chat {chat_id}")


# --------------------------------------------------------------------------
# Rotate -- Cancel (context-aware, same pattern as Split)
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_ROTATE_STATES), F.data == ROTATE_CB_CANCEL)
async def pdf_rotate_cancel_ask(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    current_state = await state.get_state()

    if current_state == RotateStates.waiting_for_file_rotate.state:
        # Nothing uploaded yet -- cancel immediately, no confirmation.
        await _rotate_full_cleanup(state, chat_id)
        await query.message.edit_text(
            "📄 PDF Toolkit -- choose an operation:",
            reply_markup=get_pdf_menu(),
        )
        return

    await state.update_data(rotate_pre_cancel_state=current_state)
    await _replace_rotate_message(
        query.bot, state, chat_id,
        "⚠️ Cancel Rotate?\n\nYour current progress will be lost.\n\nContinue?",
        _rotate_cancel_confirm_keyboard(),
    )


@router.callback_query(F.data == ROTATE_CB_CANCEL_YES)
async def pdf_rotate_cancel_yes(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    await _rotate_full_cleanup(state, chat_id)
    await query.message.edit_text("❌ Rotate cancelled.")


@router.callback_query(F.data == ROTATE_CB_CANCEL_NO)
async def pdf_rotate_cancel_no(query: CallbackQuery, state: FSMContext):
    """Restores exactly the Rotate screen the user was on before Cancel."""
    await query.answer()
    data = await state.get_data()
    prev_state = data.get("rotate_pre_cancel_state")
    await state.set_state(prev_state)
    chat_id = query.message.chat.id

    filename = data.get("rotate_filename", "document.pdf")
    page_count = data.get("rotate_page_count", 0)
    size_bytes = data.get("rotate_file_size")
    pages = data.get("rotate_pages")

    if prev_state == RotateStates.waiting_for_rotate_target.state:
        await _replace_rotate_message(
            query.bot, state, chat_id,
            _render_pdf_loaded_text(filename, page_count, size_bytes), _rotate_target_keyboard(),
        )
    elif prev_state == RotateStates.waiting_for_rotate_pages_input.state:
        await _replace_rotate_message(
            query.bot, state, chat_id,
            _render_pages_input_text(), _rotate_pages_input_keyboard(),
        )
    elif prev_state == RotateStates.waiting_for_rotate_angle_entire.state:
        await _replace_rotate_message(
            query.bot, state, chat_id,
            _render_angle_entire_text(), _rotate_angle_keyboard(),
        )
    elif prev_state == RotateStates.waiting_for_rotate_angle_selected.state:
        await _replace_rotate_message(
            query.bot, state, chat_id,
            _render_pages_selected_text(pages or []), _rotate_angle_keyboard(),
        )
    elif prev_state == RotateStates.waiting_for_rotate_preview.state:
        angle_cb = data.get("rotate_angle_cb")
        await _replace_rotate_message(
            query.bot, state, chat_id,
            _render_preview_text(filename, page_count, pages, angle_cb), _rotate_preview_keyboard(),
        )


# --------------------------------------------------------------------------
# Stale-button safety net -- same rationale as Split's: if /start or
# another flow's Back/Home/Cancel has already cleared the FSM state, an
# old Rotate inline keyboard still on screen would otherwise spin forever
# with no reply when pressed.
# --------------------------------------------------------------------------

register_stale_callbacks(exact={
    ROTATE_CB_TARGET_ALL, ROTATE_CB_TARGET_SELECTED, ROTATE_CB_ANGLE_LEFT, ROTATE_CB_ANGLE_RIGHT,
    ROTATE_CB_ANGLE_180, ROTATE_CB_BACK, ROTATE_CB_CANCEL, ROTATE_CB_CANCEL_YES, ROTATE_CB_CANCEL_NO,
    ROTATE_CB_CONFIRM,
})
