"""Rearrange pages: upload a PDF, then send the new page order."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.states.pdf import PDFStates
from bot.keyboards.pdf import PDF_REARRANGE
from bot.keyboards.common import back_home_cancel
from core.constants import SUPPORTED_PDF_EXTS

from services.pdf.rearranger import PDFRearranger
from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count

from utils.tempfiles import track_temp_file

from .common import _PDF_MIME, _download_and_validate, _fail, _track_usage, _finish_with_document

router = Router()

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
