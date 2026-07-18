"""PDF Toolkit handlers, split into one module per operation for
maintainability. This package exposes a single combined `router`, exactly
like the previous single-file `bot/handlers/pdf.py` did -- nothing outside
this package needs to know about the internal split.
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.keyboards.pdf import get_pdf_menu
from core.constants import CB_PDF

from .merge import router as merge_router
from .split import router as split_router
from .compress import router as compress_router
from .rotate import router as rotate_router
from .extract import router as extract_router
from .rearrange import router as rearrange_router
from .watermark import router as watermark_router
from .password import router as password_router
from .image import router as image_router

router = Router()


@router.callback_query(F.data == CB_PDF)
async def pdf_menu_open(query: CallbackQuery, state: FSMContext):
    """Opens the PDF submenu from the main menu's "📄 PDF" button."""
    await query.message.edit_text(
        "📄 PDF Toolkit -- choose an operation:",
        reply_markup=get_pdf_menu(),
    )
    await query.answer()


router.include_router(merge_router)
router.include_router(split_router)
router.include_router(compress_router)
router.include_router(rotate_router)
router.include_router(extract_router)
router.include_router(rearrange_router)
router.include_router(watermark_router)
router.include_router(password_router)
router.include_router(image_router)
