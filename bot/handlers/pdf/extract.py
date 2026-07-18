"""Extract pages: upload a PDF, then send the page ranges to extract."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.states.pdf import PDFStates
from bot.keyboards.pdf import PDF_EXTRACT
from bot.keyboards.common import back_home_cancel
from core.constants import SUPPORTED_PDF_EXTS

from services.pdf.extractor import PDFExtractor
from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count

from utils.tempfiles import track_temp_file

from .common import _PDF_MIME, _download_and_validate, _fail, _track_usage, _finish_with_document

router = Router()

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
