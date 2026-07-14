from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import core.constants as const

IMG_COMPRESS = "img_compress"
IMG_RESIZE = "img_resize"
IMG_CROP = "img_crop"
IMG_ROTATE = "img_rotate"
IMG_FLIP = "img_flip"
IMG_CONVERT = "img_convert"
IMG_BG_REMOVE = "img_bg_remove"
IMG_WATERMARK = "img_watermark"
IMG_METADATA_REMOVE = "img_meta_remove"

IMG_COMPRESS_LOW = "img_cl_low"
IMG_COMPRESS_MEDIUM = "img_cl_med"
IMG_COMPRESS_HIGH = "img_cl_high"

IMG_ROTATE_90 = "img_rot_90"
IMG_ROTATE_180 = "img_rot_180"
IMG_ROTATE_270 = "img_rot_270"

IMG_FLIP_H = "img_flip_h"
IMG_FLIP_V = "img_flip_v"

IMG_CONVERT_JPEG = "img_cv_jpeg"
IMG_CONVERT_PNG = "img_cv_png"
IMG_CONVERT_WEBP = "img_cv_webp"
IMG_CONVERT_BMP = "img_cv_bmp"


def get_image_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📦 Compress", callback_data=IMG_COMPRESS)],
        [InlineKeyboardButton(text="📐 Resize", callback_data=IMG_RESIZE)],
        [InlineKeyboardButton(text="✂️ Crop", callback_data=IMG_CROP)],
        [InlineKeyboardButton(text="🔄 Rotate", callback_data=IMG_ROTATE)],
        [InlineKeyboardButton(text="🔁 Flip", callback_data=IMG_FLIP)],
        [InlineKeyboardButton(text="🔀 Convert Format", callback_data=IMG_CONVERT)],
        [InlineKeyboardButton(text="🪄 Remove Background", callback_data=IMG_BG_REMOVE)],
        [InlineKeyboardButton(text="💧 Watermark", callback_data=IMG_WATERMARK)],
        [InlineKeyboardButton(text="🧹 Remove Metadata", callback_data=IMG_METADATA_REMOVE)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def compression_level_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Low (best quality)", callback_data=IMG_COMPRESS_LOW)],
        [InlineKeyboardButton(text="🟡 Medium (balanced)", callback_data=IMG_COMPRESS_MEDIUM)],
        [InlineKeyboardButton(text="🔴 High (smallest size)", callback_data=IMG_COMPRESS_HIGH)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])


def rotate_angle_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↻ 90°", callback_data=IMG_ROTATE_90)],
        [InlineKeyboardButton(text="↻ 180°", callback_data=IMG_ROTATE_180)],
        [InlineKeyboardButton(text="↻ 270°", callback_data=IMG_ROTATE_270)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])


def flip_direction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↔️ Horizontal", callback_data=IMG_FLIP_H)],
        [InlineKeyboardButton(text="↕️ Vertical", callback_data=IMG_FLIP_V)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])


def convert_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="JPEG", callback_data=IMG_CONVERT_JPEG)],
        [InlineKeyboardButton(text="PNG", callback_data=IMG_CONVERT_PNG)],
        [InlineKeyboardButton(text="WEBP", callback_data=IMG_CONVERT_WEBP)],
        [InlineKeyboardButton(text="BMP", callback_data=IMG_CONVERT_BMP)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])
