"""Watermark PDF: upload -> choose Text or Image watermark -> configure
position/rotation/opacity/size -> review Summary -> Apply.

Mirrors the project's existing PDF-tool UX philosophy exactly (see Merge /
Rotate / Rearrange): a single, repeatedly-edited bot message drives the
whole flow, uploads and user input are deleted as soon as they're consumed,
validation errors are temporary, Back is context-aware and never loses
previous selections, and Cancel asks for confirmation before discarding
anything.

Engine: PyMuPDF (fitz) only -- see services.pdf.watermark.
"""
import asyncio
import re
from typing import Dict, List

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.pdf import get_pdf_menu, PDF_WATERMARK
from core.constants import SUPPORTED_PDF_EXTS, MERGE_BATCH_FINALIZE_DELAY_SECONDS
from core.logger import logger

from services.pdf._common import PDFProcessingError, open_pdf_reader
from services.pdf.watermark import PDFWatermark

from utils.tempfiles import (
    new_temp_path,
    track_temp_file,
    untrack_temp_files,
    delete_paths,
    get_tracked_files,
)
from utils.validators import validate_extension
from utils.session_manager import register_stale_callbacks

from .common import _PDF_MIME, _download_and_validate, _track_usage, _display_name

router = Router()

# --------------------------------------------------------------------------
# FSM (self-contained StatesGroup, same convention as Rearrange -- nothing
# outside this file needs to know about it, and the generic /start reset in
# bot.handlers.base works on tracked temp files + state.clear() regardless
# of which StatesGroup a state belongs to.)
# --------------------------------------------------------------------------


class WatermarkStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_type = State()

    # Text watermark branch
    waiting_for_text = State()
    waiting_for_position = State()
    waiting_for_rotation = State()
    waiting_for_opacity = State()
    waiting_for_opacity_custom = State()
    waiting_for_color = State()
    waiting_for_style = State()
    waiting_for_size = State()
    waiting_for_summary = State()

    # Image watermark branch
    waiting_for_image = State()
    waiting_for_image_position = State()
    waiting_for_image_size = State()
    waiting_for_image_opacity = State()
    waiting_for_image_opacity_custom = State()
    waiting_for_image_summary = State()

    waiting_for_cancel_confirm = State()


_ALL_WM_STATES = (
    WatermarkStates.waiting_for_file,
    WatermarkStates.waiting_for_type,
    WatermarkStates.waiting_for_text,
    WatermarkStates.waiting_for_position,
    WatermarkStates.waiting_for_rotation,
    WatermarkStates.waiting_for_opacity,
    WatermarkStates.waiting_for_opacity_custom,
    WatermarkStates.waiting_for_color,
    WatermarkStates.waiting_for_style,
    WatermarkStates.waiting_for_size,
    WatermarkStates.waiting_for_summary,
    WatermarkStates.waiting_for_image,
    WatermarkStates.waiting_for_image_position,
    WatermarkStates.waiting_for_image_size,
    WatermarkStates.waiting_for_image_opacity,
    WatermarkStates.waiting_for_image_opacity_custom,
    WatermarkStates.waiting_for_image_summary,
    WatermarkStates.waiting_for_cancel_confirm,
)

# --------------------------------------------------------------------------
# Callback data
# --------------------------------------------------------------------------

WM_CB_CANCEL = "pdfwm:cancel"
WM_CB_CANCEL_YES = "pdfwm:cancel_yes"
WM_CB_CANCEL_NO = "pdfwm:cancel_no"
WM_CB_BACK = "pdfwm:back"

WM_CB_TYPE_TEXT = "pdfwm:type_text"
WM_CB_TYPE_IMAGE = "pdfwm:type_image"

WM_CB_POS_PREFIX = "pdfwm:pos:"
WM_CB_ROT_PREFIX = "pdfwm:rot:"
WM_CB_OPA_PREFIX = "pdfwm:opa:"
WM_CB_OPA_CUSTOM = "pdfwm:opa_custom"
WM_CB_COLOR_PREFIX = "pdfwm:color:"
WM_CB_STYLE_PREFIX = "pdfwm:style:"
WM_CB_SIZE_PREFIX = "pdfwm:size:"

WM_CB_SUM_POSITION = "pdfwm:sum_position"
WM_CB_SUM_ROTATION = "pdfwm:sum_rotation"
WM_CB_SUM_OPACITY = "pdfwm:sum_opacity"
WM_CB_SUM_COLOR = "pdfwm:sum_color"
WM_CB_SUM_STYLE = "pdfwm:sum_style"
WM_CB_SUM_SIZE = "pdfwm:sum_size"
WM_CB_SUM_EDIT_TEXT = "pdfwm:sum_edit_text"
WM_CB_SUM_CHANGE_IMAGE = "pdfwm:sum_change_image"
WM_CB_APPLY = "pdfwm:apply"

register_stale_callbacks(prefix="pdfwm:")

_IMAGE_WM_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_IMAGE_WM_MIMES = {"image/png", "image/jpeg", "image/webp"}

# Buffers for the case a PDF arrives as part of a Telegram media group
# (album) -- Watermark only ever accepts ONE PDF, so a whole album must be
# rejected as a unit. Mirrors Rearrange's buffer-then-debounce approach.
_wm_pending_groups: Dict[str, List[Message]] = {}
_wm_group_tasks: Dict[str, "asyncio.Task"] = {}

_POSITION_LABELS = {
    "center": "Center",
    "top_left": "Top Left",
    "top_right": "Top Right",
    "bottom_left": "Bottom Left",
    "bottom_right": "Bottom Right",
    "header": "Header",
    "footer": "Footer",
}
_ROTATION_LABELS = {
    "diagonal": "Diagonal",
    "reverse_diagonal": "Reverse Diagonal",
    "horizontal": "Horizontal",
    "vertical": "Vertical",
}
_TEXT_SIZE_LABELS = {"small": "Small", "medium": "Medium", "large": "Large", "auto": "Auto"}
_IMAGE_SIZE_LABELS = {"small": "Small", "medium": "Medium", "large": "Large", "original": "Original"}

_COLOR_LABELS = {
    "black": "⚫ Black",
    "white": "⚪ White",
    "red": "🔴 Red",
    "blue": "🔵 Blue",
    "green": "🟢 Green",
    "yellow": "🟡 Yellow",
    "purple": "🟣 Purple",
    "orange": "🟠 Orange",
    "brown": "🟤 Brown",
    "pink": "🌸 Pink",
}
_STYLE_LABELS = {
    "normal": "Normal",
    "bold": "Bold",
    "italic": "Italic",
    "bold_italic": "Bold Italic",
}

_DEFAULTS = {
    "wm_position": "center",
    "wm_rotation": "diagonal",
    "wm_opacity": 25,
    "wm_color": "black",
    "wm_style": "normal",
    "wm_size": "auto",
}
_IMAGE_DEFAULTS = {
    "wm_position": "bottom_right",
    "wm_size": "medium",
    "wm_opacity": 50,
}


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------

def _upload_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _type_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📝 Text Watermark", callback_data=WM_CB_TYPE_TEXT)
    b.button(text="🖼 Image Watermark", callback_data=WM_CB_TYPE_IMAGE)
    b.button(text="↩ Back", callback_data=WM_CB_BACK)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def _back_cancel_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="↩ Back", callback_data=WM_CB_BACK)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(1, 1)
    return b.as_markup()


def _position_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="◻ Center", callback_data=f"{WM_CB_POS_PREFIX}center")
    b.button(text="↖ Top Left", callback_data=f"{WM_CB_POS_PREFIX}top_left")
    b.button(text="↗ Top Right", callback_data=f"{WM_CB_POS_PREFIX}top_right")
    b.button(text="↙ Bottom Left", callback_data=f"{WM_CB_POS_PREFIX}bottom_left")
    b.button(text="↘ Bottom Right", callback_data=f"{WM_CB_POS_PREFIX}bottom_right")
    b.button(text="⬆ Header", callback_data=f"{WM_CB_POS_PREFIX}header")
    b.button(text="⬇ Footer", callback_data=f"{WM_CB_POS_PREFIX}footer")
    b.button(text="↩ Back", callback_data=WM_CB_BACK)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(1, 2, 2, 2, 1, 1)
    return b.as_markup()


def _rotation_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="╲ Diagonal", callback_data=f"{WM_CB_ROT_PREFIX}diagonal")
    b.button(text="╱ Reverse Diagonal", callback_data=f"{WM_CB_ROT_PREFIX}reverse_diagonal")
    b.button(text="─ Horizontal", callback_data=f"{WM_CB_ROT_PREFIX}horizontal")
    b.button(text="│ Vertical", callback_data=f"{WM_CB_ROT_PREFIX}vertical")
    b.button(text="↩ Back", callback_data=WM_CB_BACK)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def _opacity_keyboard():
    b = InlineKeyboardBuilder()
    for pct in (10, 25, 50, 75, 100):
        b.button(text=f"{pct}%", callback_data=f"{WM_CB_OPA_PREFIX}{pct}")
    b.button(text="✏ Custom", callback_data=WM_CB_OPA_CUSTOM)
    b.button(text="↩ Back", callback_data=WM_CB_BACK)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(2, 2, 2, 1, 1)
    return b.as_markup()


def _color_keyboard():
    b = InlineKeyboardBuilder()
    for key in ("black", "white", "red", "blue", "green", "yellow", "purple", "orange", "brown", "pink"):
        b.button(text=_COLOR_LABELS[key], callback_data=f"{WM_CB_COLOR_PREFIX}{key}")
    b.button(text="↩ Back", callback_data=WM_CB_BACK)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(2, 2, 2, 2, 2, 1, 1)
    return b.as_markup()


def _style_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="Normal", callback_data=f"{WM_CB_STYLE_PREFIX}normal")
    b.button(text="Bold", callback_data=f"{WM_CB_STYLE_PREFIX}bold")
    b.button(text="Italic", callback_data=f"{WM_CB_STYLE_PREFIX}italic")
    b.button(text="Bold Italic", callback_data=f"{WM_CB_STYLE_PREFIX}bold_italic")
    b.button(text="↩ Back", callback_data=WM_CB_BACK)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def _text_size_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="Small", callback_data=f"{WM_CB_SIZE_PREFIX}small")
    b.button(text="Medium", callback_data=f"{WM_CB_SIZE_PREFIX}medium")
    b.button(text="Large", callback_data=f"{WM_CB_SIZE_PREFIX}large")
    b.button(text="Auto", callback_data=f"{WM_CB_SIZE_PREFIX}auto")
    b.button(text="↩ Back", callback_data=WM_CB_BACK)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def _image_size_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="Small", callback_data=f"{WM_CB_SIZE_PREFIX}small")
    b.button(text="Medium", callback_data=f"{WM_CB_SIZE_PREFIX}medium")
    b.button(text="Large", callback_data=f"{WM_CB_SIZE_PREFIX}large")
    b.button(text="Original", callback_data=f"{WM_CB_SIZE_PREFIX}original")
    b.button(text="↩ Back", callback_data=WM_CB_BACK)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def _text_summary_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📍 Position", callback_data=WM_CB_SUM_POSITION)
    b.button(text="🔄 Rotation", callback_data=WM_CB_SUM_ROTATION)
    b.button(text="👁 Opacity", callback_data=WM_CB_SUM_OPACITY)
    b.button(text="🎨 Color", callback_data=WM_CB_SUM_COLOR)
    b.button(text="🔠 Style", callback_data=WM_CB_SUM_STYLE)
    b.button(text="📏 Size", callback_data=WM_CB_SUM_SIZE)
    b.button(text="✏ Edit Text", callback_data=WM_CB_SUM_EDIT_TEXT)
    b.button(text="✅ Apply", callback_data=WM_CB_APPLY)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(2, 2, 2, 1, 1, 1)
    return b.as_markup()


def _image_summary_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📍 Position", callback_data=WM_CB_SUM_POSITION)
    b.button(text="📏 Size", callback_data=WM_CB_SUM_SIZE)
    b.button(text="👁 Opacity", callback_data=WM_CB_SUM_OPACITY)
    b.button(text="🖼 Change Image", callback_data=WM_CB_SUM_CHANGE_IMAGE)
    b.button(text="✅ Apply", callback_data=WM_CB_APPLY)
    b.button(text="❌ Cancel", callback_data=WM_CB_CANCEL)
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def _cancel_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yes, Cancel", callback_data=WM_CB_CANCEL_YES)
    b.button(text="↩ No, Continue", callback_data=WM_CB_CANCEL_NO)
    b.adjust(1, 1)
    return b.as_markup()


# --------------------------------------------------------------------------
# Screen text renderers
# --------------------------------------------------------------------------

def _render_upload_text() -> str:
    return "💧 Watermark PDF\n\nAdd a text or image watermark to every page.\n\nPlease send the PDF."


def _render_type_text() -> str:
    return "✅ PDF received.\n\nChoose watermark type."


def _render_text_prompt() -> str:
    return (
        "📝 Text Watermark\n\n"
        "Send the text you want to use.\n\n"
        "Example:\n"
        "CONFIDENTIAL\n"
        "DRAFT\n"
        "John Doe"
    )


def _render_position_text() -> str:
    return "Choose watermark position."


def _render_rotation_text() -> str:
    return "Choose rotation."


def _render_opacity_text() -> str:
    return "Choose opacity."


def _render_opacity_custom_text() -> str:
    return "✏ Custom Opacity\n\nEnter opacity (1–100)."


def _render_color_text() -> str:
    return "Choose watermark text color."


def _render_style_text() -> str:
    return "Choose watermark text style."


def _render_text_size_text() -> str:
    return "Choose text size."


def _render_text_summary(data: dict) -> str:
    return (
        "💧 Watermark Settings\n\n"
        "Type:\n"
        "Text\n\n"
        "Content:\n"
        f"{data.get('wm_text', '')}\n\n"
        "Position:\n"
        f"{_POSITION_LABELS.get(data.get('wm_position'), '-')}\n\n"
        "Rotation:\n"
        f"{_ROTATION_LABELS.get(data.get('wm_rotation'), '-')}\n\n"
        "Opacity:\n"
        f"{data.get('wm_opacity')}%\n\n"
        "Color:\n"
        f"{_COLOR_LABELS.get(data.get('wm_color'), '-')}\n\n"
        "Style:\n"
        f"{_STYLE_LABELS.get(data.get('wm_style'), '-')}\n\n"
        "Size:\n"
        f"{_TEXT_SIZE_LABELS.get(data.get('wm_size'), '-')}"
    )


def _render_image_prompt() -> str:
    return (
        "🖼 Image Watermark\n\n"
        "Send your watermark image.\n\n"
        "Supported:\n\n"
        "• Telegram Photo\n\n"
        "• PNG\n\n"
        "• JPG\n\n"
        "• JPEG\n\n"
        "• WEBP"
    )


def _render_image_size_text() -> str:
    return "Choose image size."


def _render_image_summary(data: dict) -> str:
    return (
        "💧 Watermark Settings\n\n"
        "Type:\n"
        "Image\n\n"
        "Image:\n"
        f"{_display_name(data.get('wm_image_name', 'image'))}\n\n"
        "Position:\n"
        f"{_POSITION_LABELS.get(data.get('wm_position'), '-')}\n\n"
        "Size:\n"
        f"{_IMAGE_SIZE_LABELS.get(data.get('wm_size'), '-')}\n\n"
        "Opacity:\n"
        f"{data.get('wm_opacity')}%"
    )


def _render_cancel_confirm_text() -> str:
    return "❓ Cancel Watermark?\n\nYour current progress will be lost."


# --------------------------------------------------------------------------
# Prompt edit/send helper (edit existing bot message whenever possible)
# --------------------------------------------------------------------------

async def _show(bot, state: FSMContext, chat_id: int, text: str, keyboard=None) -> None:
    data = await state.get_data()
    message_id = data.get("wm_prompt_message_id")
    if message_id is not None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
            return
        except Exception as e:
            logger.debug(f"Watermark: edit failed, sending new prompt: {e}")

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await state.update_data(wm_prompt_message_id=sent.message_id)


_VALIDATION_ERROR_TTL_SECONDS = 7


async def _send_temp_validation_error(bot, chat_id: int, text: str) -> None:
    try:
        sent = await bot.send_message(chat_id, text)
    except Exception as e:
        logger.debug(f"Watermark: could not send temporary validation error: {e}")
        return

    async def _delete_later():
        await asyncio.sleep(_VALIDATION_ERROR_TTL_SECONDS)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
        except Exception as e:
            logger.debug(f"Watermark: could not auto-delete validation error: {e}")

    asyncio.create_task(_delete_later())


async def _delete_message_silently(message: Message) -> None:
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Watermark: could not delete message: {e}")


async def _full_cleanup(state: FSMContext) -> None:
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


def _watermarked_filename(name: str) -> str:
    """Apply the project's `_suffix` naming convention: insert `_wm`
    before the final extension (e.g. Report.pdf -> Report_wm.pdf,
    invoice.v2.final.pdf -> invoice.v2.final_wm.pdf).
    """
    stem, dot, ext = (name or "document.pdf").rpartition(".")
    if not dot:
        return f"{name}_wm.pdf"
    return f"{stem}_wm.{ext}"


# --------------------------------------------------------------------------
# Step 1: entry + PDF upload
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_WATERMARK)
async def pdf_watermark_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(WatermarkStates.waiting_for_file)
    await query.message.edit_text(_render_upload_text(), reply_markup=_upload_keyboard())
    await state.update_data(
        wm_prompt_message_id=query.message.message_id,
    )
    await query.answer()


async def _process_single_wm_pdf(message: Message, state: FSMContext, db_user=None) -> None:
    doc = message.document
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await _delete_message_silently(message)
        await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please send a valid PDF document.")
        return

    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        await _delete_message_silently(message)
        await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please send a valid PDF document.")
        await _show(message.bot, state, message.chat.id, _render_upload_text(), _upload_keyboard())
        return

    try:
        reader = open_pdf_reader(path)
        _ = len(reader.pages)
    except PDFProcessingError as e:
        await untrack_temp_files(state, [path])
        delete_paths([path])
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        await _show(message.bot, state, message.chat.id, _render_upload_text(), _upload_keyboard())
        return

    await state.update_data(wm_input_path=path, wm_filename=doc.file_name or "document.pdf")
    await state.set_state(WatermarkStates.waiting_for_type)

    # Remove the initial upload prompt -- the uploaded PDF stays, but the
    # "Please send the PDF." message must not linger, matching Rotate/
    # Split/Compress/etc's clean-chat UX.
    data = await state.get_data()
    old_prompt_id = data.get("wm_prompt_message_id")
    if old_prompt_id is not None:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=old_prompt_id)
        except Exception as e:
            logger.debug(f"Watermark: could not delete initial upload prompt: {e}")

    # Send as a NEW message rather than editing the original upload prompt --
    # matches Rotate/Split/Compress/etc: the upload prompt is removed, and
    # this new message (appearing below the user's PDF) becomes the single
    # message the rest of the flow edits in place.
    sent = await message.answer(_render_type_text(), reply_markup=_type_keyboard())
    await state.update_data(wm_prompt_message_id=sent.message_id)


async def _finalize_wm_media_group(key: str, state: FSMContext, db_user) -> None:
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    group = _wm_pending_groups.pop(key, None)
    _wm_group_tasks.pop(key, None)
    if not group:
        return
    if await state.get_state() != WatermarkStates.waiting_for_file.state:
        return  # user navigated away while the album was still arriving

    if len(group) > 1:
        for m in group:
            await _delete_message_silently(m)
        await _send_temp_validation_error(group[0].bot, group[0].chat.id, "❌ Please send only one PDF.")
        return

    await _process_single_wm_pdf(group[0], state, db_user=db_user)


@router.message(WatermarkStates.waiting_for_file, F.document)
async def pdf_watermark_receive_pdf(message: Message, state: FSMContext, db_user=None):
    if message.media_group_id:
        key = f"{message.chat.id}:{message.media_group_id}"
        group = _wm_pending_groups.setdefault(key, [])
        group.append(message)
        old_task = _wm_group_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        _wm_group_tasks[key] = asyncio.create_task(
            _finalize_wm_media_group(key, state, db_user)
        )
        return

    await _process_single_wm_pdf(message, state, db_user=db_user)


@router.message(WatermarkStates.waiting_for_file)
async def pdf_watermark_receive_pdf_invalid(message: Message):
    await _delete_message_silently(message)
    await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please send a valid PDF document.")


# --------------------------------------------------------------------------
# Step 2: choose watermark type
# --------------------------------------------------------------------------

@router.callback_query(WatermarkStates.waiting_for_type, F.data == WM_CB_TYPE_TEXT)
async def pdf_watermark_choose_text(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_type="text", **_DEFAULTS)
    await state.set_state(WatermarkStates.waiting_for_text)
    await _show(query.bot, state, query.message.chat.id, _render_text_prompt(), _back_cancel_keyboard())


@router.callback_query(WatermarkStates.waiting_for_type, F.data == WM_CB_TYPE_IMAGE)
async def pdf_watermark_choose_image(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_type="image", **_IMAGE_DEFAULTS)
    await state.set_state(WatermarkStates.waiting_for_image)
    await _show(query.bot, state, query.message.chat.id, _render_image_prompt(), _back_cancel_keyboard())


# --------------------------------------------------------------------------
# Text watermark: text input
# --------------------------------------------------------------------------

@router.message(WatermarkStates.waiting_for_text, F.text)
async def pdf_watermark_text_received(message: Message, state: FSMContext):
    text = message.text.strip()
    await _delete_message_silently(message)

    if not text:
        await _send_temp_validation_error(message.bot, message.chat.id, "❌ Watermark text cannot be empty.")
        return
    if len(text) > 100:
        await _send_temp_validation_error(
            message.bot, message.chat.id, "❌ Watermark text is too long.\n\nMaximum 100 characters."
        )
        return

    await state.update_data(wm_text=text)
    data = await state.get_data()

    if data.get("wm_edit_return"):
        await state.update_data(wm_edit_return=False)
        await state.set_state(WatermarkStates.waiting_for_summary)
        fresh = await state.get_data()
        await _show(message.bot, state, message.chat.id, _render_text_summary(fresh), _text_summary_keyboard())
        return

    await state.set_state(WatermarkStates.waiting_for_position)
    await _show(message.bot, state, message.chat.id, _render_position_text(), _position_keyboard())


@router.message(WatermarkStates.waiting_for_text)
async def pdf_watermark_text_invalid(message: Message):
    await _delete_message_silently(message)
    await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please send text only.")


# --------------------------------------------------------------------------
# Text watermark: position -> rotation -> opacity -> size -> summary
# --------------------------------------------------------------------------

async def _after_field_selected(query: CallbackQuery, state: FSMContext, summary_state, summary_renderer,
                                 summary_keyboard, next_state, next_text, next_keyboard) -> None:
    """Shared 'advance or return to summary' logic used by every
    position/rotation/opacity/size selection handler in both branches.
    """
    data = await state.get_data()
    if data.get("wm_edit_return"):
        await state.update_data(wm_edit_return=False)
        await state.set_state(summary_state)
        fresh = await state.get_data()
        await _show(query.bot, state, query.message.chat.id, summary_renderer(fresh), summary_keyboard())
        return
    await state.set_state(next_state)
    await _show(query.bot, state, query.message.chat.id, next_text, next_keyboard)


@router.callback_query(WatermarkStates.waiting_for_position, F.data.startswith(WM_CB_POS_PREFIX))
async def pdf_watermark_position_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    position = query.data[len(WM_CB_POS_PREFIX):]
    await state.update_data(wm_position=position)
    await _after_field_selected(
        query, state,
        WatermarkStates.waiting_for_summary, _render_text_summary, _text_summary_keyboard,
        WatermarkStates.waiting_for_rotation, _render_rotation_text(), _rotation_keyboard(),
    )


@router.callback_query(WatermarkStates.waiting_for_rotation, F.data.startswith(WM_CB_ROT_PREFIX))
async def pdf_watermark_rotation_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    rotation = query.data[len(WM_CB_ROT_PREFIX):]
    await state.update_data(wm_rotation=rotation)
    await _after_field_selected(
        query, state,
        WatermarkStates.waiting_for_summary, _render_text_summary, _text_summary_keyboard,
        WatermarkStates.waiting_for_opacity, _render_opacity_text(), _opacity_keyboard(),
    )


@router.callback_query(WatermarkStates.waiting_for_opacity, F.data.startswith(WM_CB_OPA_PREFIX))
async def pdf_watermark_opacity_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    opacity = int(query.data[len(WM_CB_OPA_PREFIX):])
    await state.update_data(wm_opacity=opacity)
    await _after_field_selected(
        query, state,
        WatermarkStates.waiting_for_summary, _render_text_summary, _text_summary_keyboard,
        WatermarkStates.waiting_for_color, _render_color_text(), _color_keyboard(),
    )


@router.callback_query(WatermarkStates.waiting_for_opacity, F.data == WM_CB_OPA_CUSTOM)
async def pdf_watermark_opacity_custom_prompt(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(WatermarkStates.waiting_for_opacity_custom)
    await _show(query.bot, state, query.message.chat.id, _render_opacity_custom_text(), _back_cancel_keyboard())


def _parse_opacity_input(text: str):
    t = (text or "").strip()
    if not re.fullmatch(r"[0-9]+", t):
        return None
    value = int(t)
    if value < 1 or value > 100:
        return None
    return value


@router.message(WatermarkStates.waiting_for_opacity_custom, F.text)
async def pdf_watermark_opacity_custom_received(message: Message, state: FSMContext):
    value = _parse_opacity_input(message.text)
    await _delete_message_silently(message)
    if value is None:
        await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please enter a number between 1 and 100.")
        return

    await state.update_data(wm_opacity=value)
    data = await state.get_data()
    if data.get("wm_edit_return"):
        await state.update_data(wm_edit_return=False)
        await state.set_state(WatermarkStates.waiting_for_summary)
        fresh = await state.get_data()
        await _show(message.bot, state, message.chat.id, _render_text_summary(fresh), _text_summary_keyboard())
        return

    await state.set_state(WatermarkStates.waiting_for_color)
    await _show(message.bot, state, message.chat.id, _render_color_text(), _color_keyboard())


@router.message(WatermarkStates.waiting_for_opacity_custom)
async def pdf_watermark_opacity_custom_invalid(message: Message):
    await _delete_message_silently(message)
    await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please enter a number between 1 and 100.")


@router.callback_query(WatermarkStates.waiting_for_color, F.data.startswith(WM_CB_COLOR_PREFIX))
async def pdf_watermark_color_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    color = query.data[len(WM_CB_COLOR_PREFIX):]
    await state.update_data(wm_color=color)
    await _after_field_selected(
        query, state,
        WatermarkStates.waiting_for_summary, _render_text_summary, _text_summary_keyboard,
        WatermarkStates.waiting_for_style, _render_style_text(), _style_keyboard(),
    )


@router.callback_query(WatermarkStates.waiting_for_style, F.data.startswith(WM_CB_STYLE_PREFIX))
async def pdf_watermark_style_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    style = query.data[len(WM_CB_STYLE_PREFIX):]
    await state.update_data(wm_style=style)
    await _after_field_selected(
        query, state,
        WatermarkStates.waiting_for_summary, _render_text_summary, _text_summary_keyboard,
        WatermarkStates.waiting_for_size, _render_text_size_text(), _text_size_keyboard(),
    )


@router.callback_query(WatermarkStates.waiting_for_size, F.data.startswith(WM_CB_SIZE_PREFIX))
async def pdf_watermark_size_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    size = query.data[len(WM_CB_SIZE_PREFIX):]
    await state.update_data(wm_size=size)
    await state.update_data(wm_edit_return=False)
    await state.set_state(WatermarkStates.waiting_for_summary)
    fresh = await state.get_data()
    await _show(query.bot, state, query.message.chat.id, _render_text_summary(fresh), _text_summary_keyboard())


# --------------------------------------------------------------------------
# Text summary screen: edit buttons + Apply
# --------------------------------------------------------------------------

@router.callback_query(WatermarkStates.waiting_for_summary, F.data == WM_CB_SUM_POSITION)
async def pdf_watermark_sum_position(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_position)
    await _show(query.bot, state, query.message.chat.id, _render_position_text(), _position_keyboard())


@router.callback_query(WatermarkStates.waiting_for_summary, F.data == WM_CB_SUM_ROTATION)
async def pdf_watermark_sum_rotation(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_rotation)
    await _show(query.bot, state, query.message.chat.id, _render_rotation_text(), _rotation_keyboard())


@router.callback_query(WatermarkStates.waiting_for_summary, F.data == WM_CB_SUM_OPACITY)
async def pdf_watermark_sum_opacity(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_opacity)
    await _show(query.bot, state, query.message.chat.id, _render_opacity_text(), _opacity_keyboard())


@router.callback_query(WatermarkStates.waiting_for_summary, F.data == WM_CB_SUM_SIZE)
async def pdf_watermark_sum_size(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_size)
    await _show(query.bot, state, query.message.chat.id, _render_text_size_text(), _text_size_keyboard())


@router.callback_query(WatermarkStates.waiting_for_summary, F.data == WM_CB_SUM_COLOR)
async def pdf_watermark_sum_color(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_color)
    await _show(query.bot, state, query.message.chat.id, _render_color_text(), _color_keyboard())


@router.callback_query(WatermarkStates.waiting_for_summary, F.data == WM_CB_SUM_STYLE)
async def pdf_watermark_sum_style(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_style)
    await _show(query.bot, state, query.message.chat.id, _render_style_text(), _style_keyboard())


@router.callback_query(WatermarkStates.waiting_for_summary, F.data == WM_CB_SUM_EDIT_TEXT)
async def pdf_watermark_sum_edit_text(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_text)
    await _show(query.bot, state, query.message.chat.id, _render_text_prompt(), _back_cancel_keyboard())


# --------------------------------------------------------------------------
# Image watermark: image input (Telegram Photo OR PNG/JPG/JPEG/WEBP document)
# --------------------------------------------------------------------------

@router.message(WatermarkStates.waiting_for_image, F.photo)
async def pdf_watermark_image_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    path = new_temp_path(suffix=".jpg")
    await track_temp_file(state, path)
    await message.bot.download(photo, destination=path)
    await _delete_message_silently(message)
    await _image_received(message.bot, state, message.chat.id, path, "watermark.jpg")


@router.message(WatermarkStates.waiting_for_image, F.document)
async def pdf_watermark_image_document(message: Message, state: FSMContext, db_user=None):
    doc = message.document
    if not validate_extension(doc.file_name or "", _IMAGE_WM_EXTS):
        await _delete_message_silently(message)
        await _send_temp_validation_error(
            message.bot, message.chat.id,
            "❌ Please send a Telegram photo or PNG/JPG/JPEG/WEBP image.",
        )
        return

    path = await _download_and_validate(message, state, _IMAGE_WM_EXTS, _IMAGE_WM_MIMES, "image", db_user=db_user)
    await _delete_message_silently(message)
    if path is None:
        await _send_temp_validation_error(
            message.bot, message.chat.id,
            "❌ Please send a Telegram photo or PNG/JPG/JPEG/WEBP image.",
        )
        return

    await _image_received(message.bot, state, message.chat.id, path, doc.file_name or "image")


@router.message(WatermarkStates.waiting_for_image)
async def pdf_watermark_image_invalid(message: Message):
    await _delete_message_silently(message)
    await _send_temp_validation_error(
        message.bot, message.chat.id,
        "❌ Please send a Telegram photo or PNG/JPG/JPEG/WEBP image.",
    )


async def _image_received(bot, state: FSMContext, chat_id: int, path: str, display_name: str) -> None:
    data = await state.get_data()
    old_path = data.get("wm_image_path")
    if old_path and old_path != path:
        await untrack_temp_files(state, [old_path])
        delete_paths([old_path])

    await state.update_data(wm_image_path=path, wm_image_name=display_name)

    if data.get("wm_edit_return"):
        await state.update_data(wm_edit_return=False)
        await state.set_state(WatermarkStates.waiting_for_image_summary)
        fresh = await state.get_data()
        await _show(bot, state, chat_id, _render_image_summary(fresh), _image_summary_keyboard())
        return

    await state.set_state(WatermarkStates.waiting_for_image_position)
    await _show(bot, state, chat_id, _render_position_text(), _position_keyboard())


# --------------------------------------------------------------------------
# Image watermark: position -> size -> opacity -> summary
# --------------------------------------------------------------------------

@router.callback_query(WatermarkStates.waiting_for_image_position, F.data.startswith(WM_CB_POS_PREFIX))
async def pdf_watermark_image_position_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    position = query.data[len(WM_CB_POS_PREFIX):]
    await state.update_data(wm_position=position)
    await _after_field_selected(
        query, state,
        WatermarkStates.waiting_for_image_summary, _render_image_summary, _image_summary_keyboard,
        WatermarkStates.waiting_for_image_size, _render_image_size_text(), _image_size_keyboard(),
    )


@router.callback_query(WatermarkStates.waiting_for_image_size, F.data.startswith(WM_CB_SIZE_PREFIX))
async def pdf_watermark_image_size_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    size = query.data[len(WM_CB_SIZE_PREFIX):]
    await state.update_data(wm_size=size)
    await _after_field_selected(
        query, state,
        WatermarkStates.waiting_for_image_summary, _render_image_summary, _image_summary_keyboard,
        WatermarkStates.waiting_for_image_opacity, _render_opacity_text(), _opacity_keyboard(),
    )


@router.callback_query(WatermarkStates.waiting_for_image_opacity, F.data.startswith(WM_CB_OPA_PREFIX))
async def pdf_watermark_image_opacity_chosen(query: CallbackQuery, state: FSMContext):
    await query.answer()
    opacity = int(query.data[len(WM_CB_OPA_PREFIX):])
    await state.update_data(wm_opacity=opacity)
    await state.update_data(wm_edit_return=False)
    await state.set_state(WatermarkStates.waiting_for_image_summary)
    fresh = await state.get_data()
    await _show(query.bot, state, query.message.chat.id, _render_image_summary(fresh), _image_summary_keyboard())


@router.callback_query(WatermarkStates.waiting_for_image_opacity, F.data == WM_CB_OPA_CUSTOM)
async def pdf_watermark_image_opacity_custom_prompt(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(WatermarkStates.waiting_for_image_opacity_custom)
    await _show(query.bot, state, query.message.chat.id, _render_opacity_custom_text(), _back_cancel_keyboard())


@router.message(WatermarkStates.waiting_for_image_opacity_custom, F.text)
async def pdf_watermark_image_opacity_custom_received(message: Message, state: FSMContext):
    value = _parse_opacity_input(message.text)
    await _delete_message_silently(message)
    if value is None:
        await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please enter a number between 1 and 100.")
        return

    await state.update_data(wm_opacity=value)
    await state.update_data(wm_edit_return=False)
    await state.set_state(WatermarkStates.waiting_for_image_summary)
    fresh = await state.get_data()
    await _show(message.bot, state, message.chat.id, _render_image_summary(fresh), _image_summary_keyboard())


@router.message(WatermarkStates.waiting_for_image_opacity_custom)
async def pdf_watermark_image_opacity_custom_invalid(message: Message):
    await _delete_message_silently(message)
    await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please enter a number between 1 and 100.")


# --------------------------------------------------------------------------
# Image summary screen: edit buttons + Apply
# --------------------------------------------------------------------------

@router.callback_query(WatermarkStates.waiting_for_image_summary, F.data == WM_CB_SUM_POSITION)
async def pdf_watermark_img_sum_position(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_image_position)
    await _show(query.bot, state, query.message.chat.id, _render_position_text(), _position_keyboard())


@router.callback_query(WatermarkStates.waiting_for_image_summary, F.data == WM_CB_SUM_SIZE)
async def pdf_watermark_img_sum_size(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_image_size)
    await _show(query.bot, state, query.message.chat.id, _render_image_size_text(), _image_size_keyboard())


@router.callback_query(WatermarkStates.waiting_for_image_summary, F.data == WM_CB_SUM_OPACITY)
async def pdf_watermark_img_sum_opacity(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_image_opacity)
    await _show(query.bot, state, query.message.chat.id, _render_opacity_text(), _opacity_keyboard())


@router.callback_query(WatermarkStates.waiting_for_image_summary, F.data == WM_CB_SUM_CHANGE_IMAGE)
async def pdf_watermark_img_sum_change_image(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(wm_edit_return=True)
    await state.set_state(WatermarkStates.waiting_for_image)
    await _show(query.bot, state, query.message.chat.id, _render_image_prompt(), _back_cancel_keyboard())


# --------------------------------------------------------------------------
# Apply (both branches)
# --------------------------------------------------------------------------

@router.callback_query(
    StateFilter(WatermarkStates.waiting_for_summary, WatermarkStates.waiting_for_image_summary),
    F.data == WM_CB_APPLY,
)
async def pdf_watermark_apply(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    input_path = data.get("wm_input_path")
    wm_type = data.get("wm_type")

    if not input_path:
        await query.message.answer("Session expired, please start over.")
        await _full_cleanup(state)
        await query.bot.send_message(chat_id, "📄 PDF Toolkit -- choose an operation:", reply_markup=get_pdf_menu())
        return

    if data.get("wm_applying"):
        # Duplicate Apply press (e.g. a second tap that reached the server
        # before the keyboard removal below took effect) -- ignore it.
        return
    await state.update_data(wm_applying=True)

    # Edit current message, explicitly drop the keyboard so it can never be
    # pressed again, and prevent duplicate clicks/processing.
    try:
        await query.bot.edit_message_text(
            "⏳ Applying watermark...", chat_id=chat_id, message_id=query.message.message_id,
            reply_markup=None,
        )
    except Exception as e:
        logger.debug(f"Watermark: could not edit to processing state: {e}")

    cleanup_paths = [input_path]
    try:
        if wm_type == "text":
            output_path = await PDFWatermark().add_text_watermark(
                input_path,
                data.get("wm_text", ""),
                data.get("wm_position", "center"),
                data.get("wm_rotation", "diagonal"),
                int(data.get("wm_opacity", 25)),
                data.get("wm_size", "auto"),
                data.get("wm_color", "black"),
                data.get("wm_style", "normal"),
            )
        else:
            image_path = data.get("wm_image_path")
            if not image_path:
                raise PDFProcessingError("Watermark image is missing. Please start over.")
            cleanup_paths.append(image_path)
            output_path = await PDFWatermark().add_image_watermark(
                input_path,
                image_path,
                data.get("wm_position", "bottom_right"),
                data.get("wm_size", "medium"),
                int(data.get("wm_opacity", 50)),
            )
    except Exception as e:
        if isinstance(e, PDFProcessingError):
            logger.info(f"Watermark: processing error: {e}")
        else:
            logger.exception(f"Watermark: unexpected processing error: {e}")
        try:
            await query.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        except Exception as del_err:
            logger.debug(f"Watermark: could not delete processing message: {del_err}")
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()
        await query.bot.send_message(chat_id, "❌ Failed to apply watermark.\n\nPlease try again.")
        await query.bot.send_message(chat_id, "📄 PDF Toolkit -- choose an operation:", reply_markup=get_pdf_menu())
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    cleanup_paths.append(output_path)

    filename = data.get("wm_filename", "document.pdf")
    output_filename = _watermarked_filename(filename)

    try:
        await query.bot.send_document(chat_id, FSInputFile(output_path, filename=output_filename))
    finally:
        try:
            await query.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        except Exception as e:
            logger.debug(f"Watermark: could not delete processing message: {e}")
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()

    logger.info(f"Watermark: completed for chat {chat_id}")


# --------------------------------------------------------------------------
# Back (context-aware: goes to the immediately previous step, never
# resetting the workflow or losing previous selections)
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_ALL_WM_STATES), F.data == WM_CB_BACK)
async def pdf_watermark_back(query: CallbackQuery, state: FSMContext):
    await query.answer()
    current = await state.get_state()
    chat_id = query.message.chat.id
    data = await state.get_data()

    if current == WatermarkStates.waiting_for_type.state:
        await state.set_state(WatermarkStates.waiting_for_file)
        await _show(query.bot, state, chat_id, _render_upload_text(), _upload_keyboard())

    elif current == WatermarkStates.waiting_for_text.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_summary)
            await _show(query.bot, state, chat_id, _render_text_summary(data), _text_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_type)
            await _show(query.bot, state, chat_id, _render_type_text(), _type_keyboard())

    elif current == WatermarkStates.waiting_for_position.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_summary)
            await _show(query.bot, state, chat_id, _render_text_summary(data), _text_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_text)
            await _show(query.bot, state, chat_id, _render_text_prompt(), _back_cancel_keyboard())

    elif current == WatermarkStates.waiting_for_rotation.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_summary)
            await _show(query.bot, state, chat_id, _render_text_summary(data), _text_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_position)
            await _show(query.bot, state, chat_id, _render_position_text(), _position_keyboard())

    elif current == WatermarkStates.waiting_for_opacity.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_summary)
            await _show(query.bot, state, chat_id, _render_text_summary(data), _text_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_rotation)
            await _show(query.bot, state, chat_id, _render_rotation_text(), _rotation_keyboard())

    elif current == WatermarkStates.waiting_for_opacity_custom.state:
        await state.set_state(WatermarkStates.waiting_for_opacity)
        await _show(query.bot, state, chat_id, _render_opacity_text(), _opacity_keyboard())

    elif current == WatermarkStates.waiting_for_color.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_summary)
            await _show(query.bot, state, chat_id, _render_text_summary(data), _text_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_opacity)
            await _show(query.bot, state, chat_id, _render_opacity_text(), _opacity_keyboard())

    elif current == WatermarkStates.waiting_for_style.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_summary)
            await _show(query.bot, state, chat_id, _render_text_summary(data), _text_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_color)
            await _show(query.bot, state, chat_id, _render_color_text(), _color_keyboard())

    elif current == WatermarkStates.waiting_for_size.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_summary)
            await _show(query.bot, state, chat_id, _render_text_summary(data), _text_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_style)
            await _show(query.bot, state, chat_id, _render_style_text(), _style_keyboard())

    elif current == WatermarkStates.waiting_for_image.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_image_summary)
            await _show(query.bot, state, chat_id, _render_image_summary(data), _image_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_type)
            await _show(query.bot, state, chat_id, _render_type_text(), _type_keyboard())

    elif current == WatermarkStates.waiting_for_image_position.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_image_summary)
            await _show(query.bot, state, chat_id, _render_image_summary(data), _image_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_image)
            await _show(query.bot, state, chat_id, _render_image_prompt(), _back_cancel_keyboard())

    elif current == WatermarkStates.waiting_for_image_size.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_image_summary)
            await _show(query.bot, state, chat_id, _render_image_summary(data), _image_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_image_position)
            await _show(query.bot, state, chat_id, _render_position_text(), _position_keyboard())

    elif current == WatermarkStates.waiting_for_image_opacity.state:
        if data.get("wm_edit_return"):
            await state.update_data(wm_edit_return=False)
            await state.set_state(WatermarkStates.waiting_for_image_summary)
            await _show(query.bot, state, chat_id, _render_image_summary(data), _image_summary_keyboard())
        else:
            await state.set_state(WatermarkStates.waiting_for_image_size)
            await _show(query.bot, state, chat_id, _render_image_size_text(), _image_size_keyboard())

    elif current == WatermarkStates.waiting_for_image_opacity_custom.state:
        await state.set_state(WatermarkStates.waiting_for_image_opacity)
        await _show(query.bot, state, chat_id, _render_opacity_text(), _opacity_keyboard())


# --------------------------------------------------------------------------
# Cancel -- available from every state, requires confirmation
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_ALL_WM_STATES), F.data == WM_CB_CANCEL)
async def pdf_watermark_cancel(query: CallbackQuery, state: FSMContext):
    await query.answer()
    current = await state.get_state()
    await state.update_data(wm_pre_cancel_state=current)
    await state.set_state(WatermarkStates.waiting_for_cancel_confirm)
    await _show(query.bot, state, query.message.chat.id, _render_cancel_confirm_text(), _cancel_confirm_keyboard())


@router.callback_query(WatermarkStates.waiting_for_cancel_confirm, F.data == WM_CB_CANCEL_YES)
async def pdf_watermark_cancel_yes(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _full_cleanup(state)
    await query.message.edit_text("📄 PDF Toolkit -- choose an operation:", reply_markup=get_pdf_menu())


# Screen renderers keyed by state, used to restore the exact previous
# screen when the user chooses "No, Continue".
_SCREEN_RENDERERS = {
    WatermarkStates.waiting_for_file.state: lambda d: (_render_upload_text(), _upload_keyboard()),
    WatermarkStates.waiting_for_type.state: lambda d: (_render_type_text(), _type_keyboard()),
    WatermarkStates.waiting_for_text.state: lambda d: (_render_text_prompt(), _back_cancel_keyboard()),
    WatermarkStates.waiting_for_position.state: lambda d: (_render_position_text(), _position_keyboard()),
    WatermarkStates.waiting_for_rotation.state: lambda d: (_render_rotation_text(), _rotation_keyboard()),
    WatermarkStates.waiting_for_opacity.state: lambda d: (_render_opacity_text(), _opacity_keyboard()),
    WatermarkStates.waiting_for_opacity_custom.state: lambda d: (_render_opacity_custom_text(), _back_cancel_keyboard()),
    WatermarkStates.waiting_for_color.state: lambda d: (_render_color_text(), _color_keyboard()),
    WatermarkStates.waiting_for_style.state: lambda d: (_render_style_text(), _style_keyboard()),
    WatermarkStates.waiting_for_size.state: lambda d: (_render_text_size_text(), _text_size_keyboard()),
    WatermarkStates.waiting_for_summary.state: lambda d: (_render_text_summary(d), _text_summary_keyboard()),
    WatermarkStates.waiting_for_image.state: lambda d: (_render_image_prompt(), _back_cancel_keyboard()),
    WatermarkStates.waiting_for_image_position.state: lambda d: (_render_position_text(), _position_keyboard()),
    WatermarkStates.waiting_for_image_size.state: lambda d: (_render_image_size_text(), _image_size_keyboard()),
    WatermarkStates.waiting_for_image_opacity.state: lambda d: (_render_opacity_text(), _opacity_keyboard()),
    WatermarkStates.waiting_for_image_opacity_custom.state: lambda d: (_render_opacity_custom_text(), _back_cancel_keyboard()),
    WatermarkStates.waiting_for_image_summary.state: lambda d: (_render_image_summary(d), _image_summary_keyboard()),
}


@router.callback_query(WatermarkStates.waiting_for_cancel_confirm, F.data == WM_CB_CANCEL_NO)
async def pdf_watermark_cancel_no(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    prev_state = data.get("wm_pre_cancel_state") or WatermarkStates.waiting_for_file.state
    renderer = _SCREEN_RENDERERS.get(prev_state, _SCREEN_RENDERERS[WatermarkStates.waiting_for_file.state])
    await state.set_state(prev_state)
    text, keyboard = renderer(data)
    await _show(query.bot, state, query.message.chat.id, text, keyboard)


# --------------------------------------------------------------------------
# Button-only screens: Type, Position, Rotation, Opacity, Size, Summary
# (both branches) and the Cancel-confirm screen only ever expect an inline
# button press. Any other message received there is discarded silently --
# no validation error, no state change, screen stays exactly as it is.
# --------------------------------------------------------------------------

_BUTTON_ONLY_STATES = (
    WatermarkStates.waiting_for_type,
    WatermarkStates.waiting_for_position,
    WatermarkStates.waiting_for_rotation,
    WatermarkStates.waiting_for_opacity,
    WatermarkStates.waiting_for_color,
    WatermarkStates.waiting_for_style,
    WatermarkStates.waiting_for_size,
    WatermarkStates.waiting_for_summary,
    WatermarkStates.waiting_for_image_position,
    WatermarkStates.waiting_for_image_size,
    WatermarkStates.waiting_for_image_opacity,
    WatermarkStates.waiting_for_image_summary,
    WatermarkStates.waiting_for_cancel_confirm,
)


@router.message(StateFilter(*_BUTTON_ONLY_STATES))
async def pdf_watermark_button_only_screen_message(message: Message):
    await _delete_message_silently(message)

