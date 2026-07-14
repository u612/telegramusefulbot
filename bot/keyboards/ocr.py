from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import core.constants as const
from services.ocr.extractor import SUPPORTED_LANGUAGES

OCR_START = "ocr_start"
OCR_LANG_PREFIX = "ocr_lang_"


def get_ocr_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🔍 Extract Text from Image", callback_data=OCR_START)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def ocr_language_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"{OCR_LANG_PREFIX}{code}")]
        for code, name in SUPPORTED_LANGUAGES.items()
    ]
    buttons.append([InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)])
    buttons.append([InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)])
    buttons.append([InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
  
