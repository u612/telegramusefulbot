from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from core.constants import BACK, HOME, CANCEL, CB_BACK, CB_HOME, CB_CANCEL


def back_home_cancel() -> InlineKeyboardMarkup:
    """Keyboard with Back, Home, Cancel buttons."""
    buttons = [
        [InlineKeyboardButton(text=BACK, callback_data=CB_BACK)],
        [InlineKeyboardButton(text=HOME, callback_data=CB_HOME)],
        [InlineKeyboardButton(text=CANCEL, callback_data=CB_CANCEL)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_back_button() -> InlineKeyboardMarkup:
    """Just a back button."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=BACK, callback_data=CB_BACK)]
    ])


def get_home_button() -> InlineKeyboardMarkup:
    """Just a home button."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=HOME, callback_data=CB_HOME)]
    ])
