from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import core.constants as const

ARC_COMPRESS = "arc_compress"
ARC_EXTRACT = "arc_extract"
ARC_DONE = "arc_done"

ARC_FORMAT_ZIP = "arc_fmt_zip"
ARC_FORMAT_7Z = "arc_fmt_7z"


def get_archive_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📦 Compress (create archive)", callback_data=ARC_COMPRESS)],
        [InlineKeyboardButton(text="📂 Extract", callback_data=ARC_EXTRACT)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def archive_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ZIP", callback_data=ARC_FORMAT_ZIP)],
        [InlineKeyboardButton(text="7Z", callback_data=ARC_FORMAT_7Z)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])


def archive_upload_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Done, create archive", callback_data=ARC_DONE)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])
