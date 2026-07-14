from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from core.constants import MAIN_MENU, CB_PDF, CB_IMAGE, CB_ARCHIVE, CB_DOCUMENT, CB_OCR, CB_SETTINGS


def get_main_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📄 PDF", callback_data=CB_PDF)],
        [InlineKeyboardButton(text="🖼 Images", callback_data=CB_IMAGE)],
        [InlineKeyboardButton(text="📦 Archives", callback_data=CB_ARCHIVE)],
        [InlineKeyboardButton(text="📃 Documents", callback_data=CB_DOCUMENT)],
        [InlineKeyboardButton(text="🔍 OCR", callback_data=CB_OCR)],
        [InlineKeyboardButton(text="⚙ Settings", callback_data=CB_SETTINGS)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
