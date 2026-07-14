from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import core.constants as const

DOC_CONVERT_TO_PDF = "doc_to_pdf"


def get_document_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📄 Convert to PDF", callback_data=DOC_CONVERT_TO_PDF)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
