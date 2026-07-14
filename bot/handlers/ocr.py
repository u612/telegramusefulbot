"""Handlers for OCR text extraction."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.states.ocr import OCRStates
from bot.keyboards.ocr import OCR_START, OCR_LANG_PREFIX, ocr_language_keyboard
from bot.keyboards.common import back_home_cancel
from core.constants import SUPPORTED_IMAGE_EXTS
from core.config import settings
from core.logger import logger

from services.ocr.extractor import OCRExtractor, OCRProcessingError, SUPPORTED_LANGUAGES
from utils.tempfiles import new_temp_path, track_temp_file, untrack_temp_files, delete_paths
from utils.validators import validate_extension, validate_upload

router = Router()

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif"}


async def _track_usage(user_repo, db_user) -> None:
    if user_repo is not None and db_user is not None:
        try:
            await user_repo.increment_usage(db_user.telegram_id)
        except Exception:
            logger.exception("Failed to increment usage counter (non-fatal).")


@router.callback_query(F.data == OCR_START)
async def ocr_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(OCRStates.waiting_for_language)
    await query.message.edit_text("Choose the text language in the image:", reply_markup=ocr_language_keyboard())
    await query.answer()


@router.callback_query(
    OCRStates.waiting_for_language,
    F.data.startswith(OCR_LANG_PREFIX),
)
async def ocr_language_chosen(query: CallbackQuery, state: FSMContext):
    lang = query.data[len(OCR_LANG_PREFIX):]
    if lang not in SUPPORTED_LANGUAGES:
        await query.answer("Unsupported language.", show_alert=True)
        return
    await state.update_data(ocr_lang=lang)
    await state.set_state(OCRStates.waiting_for_image)
    await query.message.edit_text("Send the image to extract text from.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(OCRStates.waiting_for_image, F.document)
async def ocr_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    doc = message.document
    if doc is None:
        await message.answer("Please send an image file as a document.")
        return
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
        await message.answer(f"File too large (max {limit_mb} MB).")
        return
    if not validate_extension(doc.file_name or "", SUPPORTED_IMAGE_EXTS):
        allowed_str = ", ".join(sorted(SUPPORTED_IMAGE_EXTS))
        await message.answer(f"Unsupported file type. Allowed: {allowed_str}")
        return

    suffix = "." + (doc.file_name or "").rsplit(".", 1)[-1].lower()
    path = new_temp_path(suffix=suffix)
    await track_temp_file(state, path)
    await message.bot.download(doc, destination=path)

    error = validate_upload(path, doc.file_name or f"file{suffix}", SUPPORTED_IMAGE_EXTS, _IMAGE_MIMES)
    if error:
        delete_paths([path])
        await untrack_temp_files(state, [path])
        await message.answer(error)
        await state.clear()
        return

    data = await state.get_data()
    lang = data.get("ocr_lang", "eng")

    await message.answer("Extracting text... please wait.")
    try:
        text = await OCRExtractor().extract_text(path, lang)
    except OCRProcessingError as e:
        await message.answer(f"⚠️ {e}")
        delete_paths([path])
        await untrack_temp_files(state, [path])
        await state.clear()
        return
    except Exception as e:
        logger.exception(f"Unexpected OCR error: {e}")
        await message.answer("Something went wrong extracting text. Please try again.")
        delete_paths([path])
        await untrack_temp_files(state, [path])
        await state.clear()
        return

    await _track_usage(user_repo, db_user)
    delete_paths([path])
    await untrack_temp_files(state, [path])
    await state.clear()

    # Telegram messages cap out at 4096 characters; split long results.
    max_len = 4000
    if len(text) <= max_len:
        await message.answer(f"📝 Extracted text:\n\n{text}")
    else:
        await message.answer("📝 Extracted text (split into multiple messages):")
        for i in range(0, len(text), max_len):
            await message.answer(text[i:i + max_len])
