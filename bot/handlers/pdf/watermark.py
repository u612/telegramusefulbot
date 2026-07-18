"""Watermark PDF: upload the file first, then send the watermark text."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.states.pdf import PDFStates
from bot.keyboards.pdf import PDF_WATERMARK
from bot.keyboards.common import back_home_cancel
from core.constants import SUPPORTED_PDF_EXTS

from services.pdf.watermark import PDFWatermark

from utils.tempfiles import track_temp_file

from .common import _PDF_MIME, _download_and_validate, _fail, _track_usage, _finish_with_document

router = Router()

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
