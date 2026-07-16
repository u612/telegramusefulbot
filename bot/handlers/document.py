"""Handlers for Document -> PDF conversion (DOCX/XLSX/PPTX/TXT/HTML/Markdown)."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext

from bot.states.document import DocumentStates
from bot.keyboards.document import get_document_menu, DOC_CONVERT_TO_PDF
from bot.keyboards.common import back_home_cancel
from core.constants import CB_DOCUMENT, SUPPORTED_DOC_EXTS
from core.logger import logger
from utils.limits import get_effective_limits

from services.document.converter import DocumentConverter, DocumentProcessingError
from utils.tempfiles import new_temp_path, track_temp_file, untrack_temp_files, delete_paths
from utils.validators import validate_extension

router = Router()


@router.callback_query(F.data == CB_DOCUMENT)
async def document_menu_open(query: CallbackQuery, state: FSMContext):
    """Opens the Document submenu from the main menu's "📃 Documents" button."""
    await query.message.edit_text(
        "📃 Document Toolkit -- choose an operation:",
        reply_markup=get_document_menu(),
    )
    await query.answer()


async def _track_usage(user_repo, db_user) -> None:
    if user_repo is not None and db_user is not None:
        try:
            await user_repo.increment_usage(db_user.telegram_id)
        except Exception:
            logger.exception("Failed to increment usage counter (non-fatal).")


@router.callback_query(F.data == DOC_CONVERT_TO_PDF)
async def doc_convert_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(DocumentStates.waiting_for_file_convert)
    allowed_str = ", ".join(sorted(SUPPORTED_DOC_EXTS))
    await query.message.edit_text(
        f"Send the document to convert to PDF. Supported: {allowed_str}",
        reply_markup=back_home_cancel(),
    )
    await query.answer()


@router.message(DocumentStates.waiting_for_file_convert, F.document)
async def doc_convert_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    doc = message.document
    if doc is None:
        await message.answer("Please send a document file.")
        return
    limits = get_effective_limits(message.from_user.id, db_user)
    if not limits.unlimited and doc.file_size and doc.file_size > limits.file_size:
        limit_mb = limits.file_size // (1024 * 1024)
        await message.answer(f"File too large (max {limit_mb} MB).")
        return
    if not validate_extension(doc.file_name or "", SUPPORTED_DOC_EXTS):
        allowed_str = ", ".join(sorted(SUPPORTED_DOC_EXTS))
        await message.answer(f"Unsupported file type. Allowed: {allowed_str}")
        return

    suffix = "." + (doc.file_name or "").rsplit(".", 1)[-1].lower()
    path = new_temp_path(suffix=suffix)
    await track_temp_file(state, path)
    await message.bot.download(doc, destination=path)

    await message.answer("Converting to PDF... this can take up to a minute, please wait.")
    try:
        output_path = await DocumentConverter().convert_to_pdf(path, timeout=limits.libreoffice_timeout)
    except DocumentProcessingError as e:
        await message.answer(f"⚠️ {e}")
        delete_paths([path])
        await untrack_temp_files(state, [path])
        await state.clear()
        return
    except Exception as e:
        logger.exception(f"Unexpected document conversion error: {e}")
        await message.answer("Something went wrong converting that file. Please try again.")
        delete_paths([path])
        await untrack_temp_files(state, [path])
        await state.clear()
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)

    cleanup_paths = [path, output_path]
    try:
        await message.answer_document(FSInputFile(output_path, filename="converted.pdf"))
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()
