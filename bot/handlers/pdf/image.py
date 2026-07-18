"""Image -> PDF: upload one or more images (as photos or documents), press
Done, combine into a PDF.
PDF -> Images: choose an output format first, then upload the PDF.
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.states.pdf import PDFStates
from bot.keyboards.pdf import (
    PDF_IMAGE_TO_PDF, PDF_PDF_TO_IMAGES, PDF_DONE,
    upload_done_keyboard, pdf_to_images_format_keyboard,
    PDF_TO_IMG_PNG, PDF_TO_IMG_JPEG,
)
from bot.keyboards.common import back_home_cancel
from core.constants import SUPPORTED_PDF_EXTS, SUPPORTED_IMAGE_EXTS
from utils.limits import get_effective_limits

from services.pdf.image_to_pdf import ImageToPDF
from services.pdf.pdf_to_images import PDFToImages

from utils.tempfiles import new_temp_path, track_temp_file, get_tracked_files

from .common import (
    _PDF_MIME, _IMAGE_MIMES,
    _download_and_validate, _fail, _track_usage,
    _finish_with_document, _finish_with_documents,
)

router = Router()

# --------------------------------------------------------------------------
# Image -> PDF (multi-file upload + Done button)
# --------------------------------------------------------------------------


@router.callback_query(F.data == PDF_IMAGE_TO_PDF)
async def pdf_image_to_pdf_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_images_to_pdf)
    await query.message.edit_text(
        "Send one or more images. Press 'Done' when finished.",
        reply_markup=upload_done_keyboard(),
    )
    await query.answer()


@router.message(PDFStates.waiting_for_images_to_pdf, F.photo)
async def pdf_image_to_pdf_receive_photo(message: Message, state: FSMContext, db_user=None):
    limits = get_effective_limits(message.from_user.id, db_user)
    current = await get_tracked_files(state)
    if not limits.unlimited and len(current) >= limits.image_to_pdf_limit:
        await message.answer(
            f"Maximum of {limits.image_to_pdf_limit} images reached. Press 'Done'."
        )
        return
    # Telegram compresses photos sent as "photo"; use the largest size.
    photo = message.photo[-1]
    path = new_temp_path(suffix=".jpg")
    await track_temp_file(state, path)
    await message.bot.download(photo, destination=path)
    count = len(await get_tracked_files(state))
    if limits.unlimited:
        await message.answer(f"Added image #{count}. Send more images or press 'Done'.")
    else:
        await message.answer(
            f"Added image {count}/{limits.image_to_pdf_limit}. Send more or press 'Done'."
        )


@router.message(PDFStates.waiting_for_images_to_pdf, F.document)
async def pdf_image_to_pdf_receive_doc(message: Message, state: FSMContext, db_user=None):
    limits = get_effective_limits(message.from_user.id, db_user)
    current = await get_tracked_files(state)
    if not limits.unlimited and len(current) >= limits.image_to_pdf_limit:
        await message.answer(
            f"Maximum of {limits.image_to_pdf_limit} images reached. Press 'Done'."
        )
        return
    path = await _download_and_validate(
        message,
        state,
        SUPPORTED_IMAGE_EXTS,
        _IMAGE_MIMES,
        "image",
        db_user=db_user,
    )
    if path is None:
        return
    count = len(await get_tracked_files(state))
    if limits.unlimited:
        await message.answer(f"Added image #{count}. Send more images or press 'Done'.")
    else:
        await message.answer(
            f"Added image {count}/{limits.image_to_pdf_limit}. Send more or press 'Done'."
        )


@router.callback_query(PDFStates.waiting_for_images_to_pdf, F.data == PDF_DONE)
async def pdf_image_to_pdf_done(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    images = await get_tracked_files(state)
    await query.answer()
    if not images:
        await query.message.answer("Send at least one image first, or press Cancel.")
        return
    limits = get_effective_limits(query.from_user.id, db_user)
    await query.message.answer("Converting... please wait.")
    try:
        output_path = await ImageToPDF().convert(images, max_images=limits.image_to_pdf_limit)
    except Exception as e:
        await _fail(query.message, state, e, images)
        return
    await _track_usage(user_repo, db_user)
    await _finish_with_document(query.message, state, output_path, "images.pdf", images + [output_path])


# --------------------------------------------------------------------------
# PDF -> Images (choose format first, then upload)
# --------------------------------------------------------------------------


@router.callback_query(F.data == PDF_PDF_TO_IMAGES)
async def pdf_to_images_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_format_pdf_to_images)
    await query.message.edit_text(
        "Choose an output image format:",
        reply_markup=pdf_to_images_format_keyboard(),
    )
    await query.answer()


@router.callback_query(
    PDFStates.waiting_for_format_pdf_to_images,
    F.data.in_({PDF_TO_IMG_PNG, PDF_TO_IMG_JPEG}),
)
async def pdf_to_images_format_chosen(query: CallbackQuery, state: FSMContext):
    fmt = "png" if query.data == PDF_TO_IMG_PNG else "jpeg"
    await state.update_data(pdf_to_images_format=fmt)
    await state.set_state(PDFStates.waiting_for_file_pdf_to_images)
    await query.message.edit_text("Send the PDF file to convert to images.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_pdf_to_images, F.document)
async def pdf_to_images_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return
    data = await state.get_data()
    fmt = data.get("pdf_to_images_format", "png")
    await message.answer("Converting to images... please wait.")
    try:
        outputs = await PDFToImages().convert(path, fmt)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    for out in outputs:
        await track_temp_file(state, out)
    ext = "jpg" if fmt == "jpeg" else "png"
    await _track_usage(user_repo, db_user)
    await _finish_with_documents(
        message, state, outputs,
        filename_fn=lambda i: f"page_{i}.{ext}",
        cleanup_paths=[path] + outputs,
    )
