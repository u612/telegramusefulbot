"""Compress PDF: upload -> analyze -> choose mode (Best Quality / Balanced /
Maximum / Target File Size) -> preview -> confirm -> compress. Everything
Compress-specific lives here.
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
from bot.keyboards.pdf import get_pdf_menu, PDF_COMPRESS
from core.constants import SUPPORTED_PDF_EXTS, MERGE_BATCH_FINALIZE_DELAY_SECONDS
from core.logger import logger
from utils.limits import get_effective_limits

from services.pdf.compressor import PDFCompressor, CompressionMode
from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count

from utils.tempfiles import track_temp_file, untrack_temp_files, get_tracked_files, delete_paths
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
        raise ValueError("Invalid target size.\n\nEnter a value smaller than the original PDF size.\n\nExample\n10")
    if value <= 0:
        raise ValueError("Invalid target size.\n\nEnter a value smaller than the original PDF size.\n\nExample\n10")
    target_bytes = int(value * 1024 * 1024)
    if original_size and target_bytes >= original_size:
        raise ValueError("Invalid target size.\n\nEnter a value smaller than the original PDF size.\n\nExample\n10")
    return target_bytes


async def _replace_compress_message(bot, state: FSMContext, chat_id: int, text: str, keyboard=None) -> None:
    """Delete the previous Compress status message (if any) and send a
    fresh one -- same pattern as Merge/Split's status message."""
    data = await state.get_data()
    old_chat_id = data.get("compress_status_chat_id")
    old_message_id = data.get("compress_status_message_id")
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Compress: status delete skipped: {e}")

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await state.update_data(compress_status_chat_id=chat_id, compress_status_message_id=sent.message_id)


async def _edit_compress_message(bot, state: FSMContext, text: str, keyboard=None) -> None:
    data = await state.get_data()
    chat_id = data.get("compress_status_chat_id")
    message_id = data.get("compress_status_message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
    except Exception as e:
        logger.debug(f"Compress: status edit skipped: {e}")


async def _compress_full_cleanup(state: FSMContext, chat_id: int) -> None:
    for key in [k for k in _compress_pending_groups if k.startswith(f"{chat_id}:")]:
        _compress_pending_groups.pop(key, None)
        task = _compress_group_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


async def _reject_compress_upload(message: Message, reason: str) -> None:
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Compress: could not delete invalid upload message: {e}")
    await _send_temp_validation_error(message.bot, message.chat.id, reason)


# --------------------------------------------------------------------------
# Compress -- Section 1/2: entry + waiting for PDF
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_COMPRESS)
async def pdf_compress_start(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    await state.set_state(PDFStates.waiting_for_file_compress)
    await query.message.edit_text(
        f"{_QUEUE_DIVIDER}\n"
        "📦 Compress PDF\n\n"
        "Send one PDF to compress.\n\n"
        "Supported methods:\n"
        "• 🟢 Best Quality\n"
        "• 🟡 Balanced\n"
        "• 🔴 Maximum Compression\n"
        "• 🎯 Target File Size\n\n"
        "📄 Send one PDF to begin.\n"
        f"{_QUEUE_DIVIDER}",
        reply_markup=_compress_upload_keyboard(),
    )
    await state.update_data(
        compress_status_chat_id=chat_id,
        compress_status_message_id=query.message.message_id,
    )
    await query.answer()


async def _process_single_compress_pdf(message: Message, state: FSMContext, db_user=None) -> None:
    """Section 3/3A/4: download + validate, show 'PDF Loaded', analyze,
    then show the method-selection screen with the analysis + recommendation.
    """
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
        compress_input_path=path,
        compress_filename=filename,
        compress_page_count=page_count,
        compress_file_size=size_bytes,
    )

    sent = await message.answer(_render_compress_loaded_text(filename, page_count, size_bytes))
    await state.update_data(compress_status_chat_id=message.chat.id, compress_status_message_id=sent.message_id)

    try:
        analysis = await PDFCompressor().analyze(path)
    except PDFProcessingError as e:
        await untrack_temp_files(state, [path])
        delete_paths([path])
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        await state.clear()
        return

    await state.update_data(
        compress_recommended=analysis.recommended.value,
        compress_doc_type=analysis.doc_type,
        compress_image_count=analysis.image_count,
        compress_text_amount=analysis.text_amount,
    )
    await state.set_state(PDFStates.waiting_for_compress_method)
    await _edit_compress_message(
        message.bot, state,
        _render_compress_analysis_text(analysis, page_count),
        _compress_method_keyboard(),
    )


async def _finalize_compress_media_group(key: str, state: FSMContext, db_user) -> None:
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    group = _compress_pending_groups.pop(key, None)
    _compress_group_tasks.pop(key, None)
    if not group:
        return
    if await state.get_state() != PDFStates.waiting_for_file_compress.state:
        return

    if len(group) > 1:
        for m in group:
            try:
                await m.delete()
            except Exception as e:
                logger.debug(f"Compress: could not delete rejected album message: {e}")
        await _send_temp_validation_error(
            group[0].bot, group[0].chat.id,
            "❌ Please send only ONE PDF.\n\nCompression works with one document at a time.",
        )
        return

    await _process_single_compress_pdf(group[0], state, db_user=db_user)


@router.message(PDFStates.waiting_for_file_compress, F.document)
async def pdf_compress_receive(message: Message, state: FSMContext, db_user=None):
    doc = message.document
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await _reject_compress_upload(message, "❌ Please send a PDF file only.")
        return

    if message.media_group_id:
        key = f"{message.chat.id}:{message.media_group_id}"
        group = _compress_pending_groups.setdefault(key, [])
        group.append(message)
        old_task = _compress_group_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        _compress_group_tasks[key] = asyncio.create_task(
            _finalize_compress_media_group(key, state, db_user)
        )
        return

    await _process_single_compress_pdf(message, state, db_user=db_user)


@router.message(PDFStates.waiting_for_file_compress)
async def pdf_compress_receive_invalid(message: Message):
    await _reject_compress_upload(message, "❌ Please send a PDF file only.")


# --------------------------------------------------------------------------
# Compress -- Section 4/5: method selection + previews
# --------------------------------------------------------------------------

async def _show_compress_preview(query: CallbackQuery, state: FSMContext, mode: CompressionMode) -> None:
    data = await state.get_data()
    await state.update_data(compress_mode=mode.value)
    await state.set_state(PDFStates.waiting_for_compress_preview)
    await _replace_compress_message(
        query.bot, state, query.message.chat.id,
        _render_compress_preview_text(
            data.get("compress_filename", "document.pdf"), data.get("compress_file_size"), mode,
        ),
        _compress_preview_keyboard(),
    )


@router.callback_query(PDFStates.waiting_for_compress_method, F.data == COMPRESS_CB_METHOD_BEST)
async def pdf_compress_choose_best(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _show_compress_preview(query, state, CompressionMode.BEST_QUALITY)


@router.callback_query(PDFStates.waiting_for_compress_method, F.data == COMPRESS_CB_METHOD_BALANCED)
async def pdf_compress_choose_balanced(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _show_compress_preview(query, state, CompressionMode.BALANCED)


@router.callback_query(PDFStates.waiting_for_compress_method, F.data == COMPRESS_CB_METHOD_MAXIMUM)
async def pdf_compress_choose_maximum(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _show_compress_preview(query, state, CompressionMode.MAXIMUM)


@router.callback_query(PDFStates.waiting_for_compress_method, F.data == COMPRESS_CB_METHOD_TARGET)
async def pdf_compress_choose_target(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    await state.set_state(PDFStates.waiting_for_compress_target_input)
    await _replace_compress_message(
        query.bot, state, query.message.chat.id,
        _render_compress_target_input_text(data.get("compress_file_size")),
        _compress_target_input_keyboard(),
    )


@router.callback_query(
    StateFilter(
        PDFStates.waiting_for_compress_preview,
        PDFStates.waiting_for_compress_target_input,
        PDFStates.waiting_for_compress_target_preview,
    ),
    F.data == COMPRESS_CB_BACK_TO_METHOD,
)
@router.callback_query(PDFStates.waiting_for_compress_preview, F.data == COMPRESS_CB_CHANGE_MODE)
async def pdf_compress_back_to_method(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    recommended = CompressionMode(data.get("compress_recommended", CompressionMode.BALANCED.value))
    await state.set_state(PDFStates.waiting_for_compress_method)
    # Re-render the analysis screen from cached numbers (no need to re-run
    # PyMuPDF -- nothing about the file has changed).
    fake_analysis = type("_A", (), {
        "doc_type": data.get("compress_doc_type", "Digital PDF"),
        "image_count": data.get("compress_image_count", 0),
        "text_amount": data.get("compress_text_amount", "Medium"),
        "recommended": recommended,
    })
    await _replace_compress_message(
        query.bot, state, query.message.chat.id,
        _render_compress_analysis_text(fake_analysis, data.get("compress_page_count", 0)),
        _compress_method_keyboard(),
    )


# --------------------------------------------------------------------------
# Compress -- Section 5D/6/7: Target File Size input, validation, preview
# --------------------------------------------------------------------------

@router.message(PDFStates.waiting_for_compress_target_input, F.text)
async def pdf_compress_target_input(message: Message, state: FSMContext):
    data = await state.get_data()
    original_size = data.get("compress_file_size")

    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Compress: could not delete target-size input message: {e}")

    try:
        target_bytes = _parse_target_size_mb(message.text, original_size)
    except ValueError as e:
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    await state.update_data(compress_target_bytes=target_bytes)
    await state.set_state(PDFStates.waiting_for_compress_target_preview)
    await _replace_compress_message(
        message.bot, state, message.chat.id,
        _render_compress_target_preview_text(original_size, target_bytes),
        _compress_target_preview_keyboard(),
    )


@router.message(PDFStates.waiting_for_compress_target_input)
async def pdf_compress_target_input_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Compress: could not delete non-text target input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id, "❌ Please send the target size as text. Example: 10"
    )


@router.callback_query(PDFStates.waiting_for_compress_target_preview, F.data == COMPRESS_CB_CHANGE_SIZE)
async def pdf_compress_change_size(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    await state.set_state(PDFStates.waiting_for_compress_target_input)
    await _replace_compress_message(
        query.bot, state, query.message.chat.id,
        _render_compress_target_input_text(data.get("compress_file_size")),
        _compress_target_input_keyboard(),
    )


# --------------------------------------------------------------------------
# Compress -- Section 8/9/10: Processing + Completion
# --------------------------------------------------------------------------

@router.callback_query(PDFStates.waiting_for_compress_preview, F.data == COMPRESS_CB_CONFIRM)
async def pdf_compress_confirm(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    path = data.get("compress_input_path")
    mode = CompressionMode(data.get("compress_mode", CompressionMode.BALANCED.value))
    if not path:
        await query.message.answer("Session expired, please start over.")
        await _compress_full_cleanup(state, chat_id)
        return

    await _replace_compress_message(query.bot, state, chat_id, "⏳ Compressing PDF...\n\nPlease wait...")

    limits = get_effective_limits(query.from_user.id, db_user)
    try:
        output_path, info = await PDFCompressor().compress(path, mode, timeout=limits.subprocess_timeout)
    except Exception as e:
        await _fail(query.message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _finish_compress(
        query.bot, state, chat_id, path, output_path, info, user_repo, db_user,
        data.get("compress_filename", "document.pdf"),
    )


@router.callback_query(PDFStates.waiting_for_compress_target_preview, F.data == COMPRESS_CB_CONFIRM_TARGET)
async def pdf_compress_confirm_target(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    path = data.get("compress_input_path")
    target_bytes = data.get("compress_target_bytes")
    if not path or not target_bytes:
        await query.message.answer("Session expired, please start over.")
        await _compress_full_cleanup(state, chat_id)
        return

    await _replace_compress_message(query.bot, state, chat_id, "⏳ Compressing PDF...\n\nPlease wait...")

    limits = get_effective_limits(query.from_user.id, db_user)
    try:
        output_path, info = await PDFCompressor().compress(
            path, CompressionMode.TARGET_SIZE, target_size_bytes=target_bytes, timeout=limits.subprocess_timeout,
        )
    except Exception as e:
        await _fail(query.message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _finish_compress(
        query.bot, state, chat_id, path, output_path, info, user_repo, db_user,
        data.get("compress_filename", "document.pdf"),
    )


def _cmp_filename(original_filename: str) -> str:
    """physics.pdf -> physics_cmp.pdf, report_final.pdf -> report_final_cmp.pdf.
    Always preserves the original stem -- never a generic name.
    """
    name = original_filename or "document.pdf"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return f"{name}_cmp.pdf"
    return f"{stem}_cmp.{ext}" if ext.lower() == "pdf" else f"{name}_cmp.pdf"


async def _finish_compress(bot, state, chat_id, input_path, output_path, info, user_repo, db_user, filename="document.pdf") -> None:
    cleanup_paths = [input_path, output_path]
    try:
        await bot.send_document(chat_id, FSInputFile(output_path, filename=_cmp_filename(filename)))
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)

    await _track_usage(user_repo, db_user)
    data = await state.get_data()
    await state.clear()
    old_chat_id = data.get("compress_status_chat_id")
    old_message_id = data.get("compress_status_message_id")
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Compress: could not delete processing message: {e}")
    await bot.send_message(chat_id, _render_compress_complete_text(info))
    logger.info(f"Compress: completed for chat {chat_id}")


# --------------------------------------------------------------------------
# Compress -- Section 11: Cancel (context-aware) / Section 12: /start is
# handled generically by base.py's _reset_to_main_menu (tracked temp files
# + FSM clear cover every Compress state the same way it covers Merge/Split).
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_COMPRESS_STATES), F.data == COMPRESS_CB_CANCEL)
async def pdf_compress_cancel_ask(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    current_state = await state.get_state()

    if current_state == PDFStates.waiting_for_file_compress.state:
        await _compress_full_cleanup(state, chat_id)
        await query.message.edit_text(
            "📄 PDF Toolkit -- choose an operation:",
            reply_markup=get_pdf_menu(),
        )
        return

    await state.update_data(compress_pre_cancel_state=current_state)
    await _replace_compress_message(
        query.bot, state, chat_id,
        "⚠️ Cancel Compression?\n\nYour current progress will be lost.\n\nContinue?",
        _compress_cancel_confirm_keyboard(),
    )


@router.callback_query(F.data == COMPRESS_CB_CANCEL_YES)
async def pdf_compress_cancel_yes(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    await _compress_full_cleanup(state, chat_id)
    await query.message.edit_text("❌ Compression cancelled.")


@router.callback_query(F.data == COMPRESS_CB_CANCEL_NO)
async def pdf_compress_cancel_no(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    prev_state = data.get("compress_pre_cancel_state")
    await state.set_state(prev_state)

    filename = data.get("compress_filename", "document.pdf")
    size_bytes = data.get("compress_file_size")

    if prev_state == PDFStates.waiting_for_compress_method.state:
        recommended = CompressionMode(data.get("compress_recommended", CompressionMode.BALANCED.value))
        fake_analysis = type("_A", (), {
            "doc_type": data.get("compress_doc_type", "Digital PDF"),
            "image_count": data.get("compress_image_count", 0),
            "text_amount": data.get("compress_text_amount", "Medium"),
            "recommended": recommended,
        })
        await _replace_compress_message(
            query.bot, state, query.message.chat.id,
            _render_compress_analysis_text(fake_analysis, data.get("compress_page_count", 0)),
            _compress_method_keyboard(),
        )
    elif prev_state == PDFStates.waiting_for_compress_preview.state:
        mode = CompressionMode(data.get("compress_mode", CompressionMode.BALANCED.value))
        await _replace_compress_message(
            query.bot, state, query.message.chat.id,
            _render_compress_preview_text(filename, size_bytes, mode), _compress_preview_keyboard(),
        )
    elif prev_state == PDFStates.waiting_for_compress_target_input.state:
        await _replace_compress_message(
            query.bot, state, query.message.chat.id,
            _render_compress_target_input_text(size_bytes), _compress_target_input_keyboard(),
        )
    elif prev_state == PDFStates.waiting_for_compress_target_preview.state:
        target_bytes = data.get("compress_target_bytes")
        await _replace_compress_message(
            query.bot, state, query.message.chat.id,
            _render_compress_target_preview_text(size_bytes, target_bytes), _compress_target_preview_keyboard(),
        )


# --------------------------------------------------------------------------
# Stale-button safety net -- same rationale as Merge/Split's.
# --------------------------------------------------------------------------

register_stale_callbacks(exact={
    COMPRESS_CB_METHOD_BEST, COMPRESS_CB_METHOD_BALANCED, COMPRESS_CB_METHOD_MAXIMUM,
    COMPRESS_CB_METHOD_TARGET, COMPRESS_CB_BACK_TO_METHOD, COMPRESS_CB_CANCEL,
    COMPRESS_CB_CANCEL_YES, COMPRESS_CB_CANCEL_NO, COMPRESS_CB_CONFIRM,
    COMPRESS_CB_CHANGE_MODE, COMPRESS_CB_CONFIRM_TARGET, COMPRESS_CB_CHANGE_SIZE,
})
