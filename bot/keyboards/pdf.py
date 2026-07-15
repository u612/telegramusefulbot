from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import core.constants as const

# --- PDF menu action callback data ---
PDF_MERGE = "pdf_merge"
PDF_SPLIT = "pdf_split"
PDF_COMPRESS = "pdf_compress"
PDF_ROTATE = "pdf_rotate"
PDF_EXTRACT = "pdf_extract"
PDF_REARRANGE = "pdf_rearrange"
PDF_WATERMARK = "pdf_watermark"
PDF_ADD_PASSWORD = "pdf_add_pass"
PDF_REMOVE_PASSWORD = "pdf_remove_pass"
PDF_IMAGE_TO_PDF = "pdf_img_to_pdf"
PDF_PDF_TO_IMAGES = "pdf_pdf_to_img"

# Shared "I'm done uploading files" action for multi-file flows (merge, image->pdf)
PDF_DONE = "pdf_done"

# Compression level choices
PDF_COMPRESS_LOW = "pdf_cl_low"
PDF_COMPRESS_MEDIUM = "pdf_cl_med"
PDF_COMPRESS_HIGH = "pdf_cl_high"

# Rotation angle choices
PDF_ROTATE_90 = "pdf_rot_90"
PDF_ROTATE_180 = "pdf_rot_180"
PDF_ROTATE_270 = "pdf_rot_270"

# PDF -> Images output format choices
PDF_TO_IMG_PNG = "pdf_p2i_png"
PDF_TO_IMG_JPEG = "pdf_p2i_jpeg"


def get_pdf_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🔄 Merge", callback_data=PDF_MERGE)],
        [InlineKeyboardButton(text="✂️ Split", callback_data=PDF_SPLIT)],
        [InlineKeyboardButton(text="📦 Compress", callback_data=PDF_COMPRESS)],
        [InlineKeyboardButton(text="🔄 Rotate", callback_data=PDF_ROTATE)],
        [InlineKeyboardButton(text="📄 Extract Pages", callback_data=PDF_EXTRACT)],
        [InlineKeyboardButton(text="🔀 Rearrange", callback_data=PDF_REARRANGE)],
        [InlineKeyboardButton(text="💧 Watermark", callback_data=PDF_WATERMARK)],
        [InlineKeyboardButton(text="🔒 Add Password", callback_data=PDF_ADD_PASSWORD)],
        [InlineKeyboardButton(text="🔓 Remove Password", callback_data=PDF_REMOVE_PASSWORD)],
        [InlineKeyboardButton(text="🖼 Image to PDF", callback_data=PDF_IMAGE_TO_PDF)],
        [InlineKeyboardButton(text="📷 PDF to Images", callback_data=PDF_PDF_TO_IMAGES)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_pdf_actions() -> InlineKeyboardMarkup:
    """Alias kept for backward compatibility with earlier imports."""
    return get_pdf_menu()


def upload_done_keyboard() -> InlineKeyboardMarkup:
    """For multi-file upload flows (Merge, Image->PDF): a Done button plus
    the standard Back/Home/Cancel.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Done, process now", callback_data=PDF_DONE)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])


def merge_queue_keyboard() -> InlineKeyboardMarkup:
    """Buttons shown on the single, repeatedly-edited Merge queue status
    message: Done / Cancel. Users simply send more files to add them --
    no separate "keep adding" acknowledgement button needed.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Done", callback_data=PDF_DONE)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])


def compression_level_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Low (best quality)", callback_data=PDF_COMPRESS_LOW)],
        [InlineKeyboardButton(text="🟡 Medium (balanced)", callback_data=PDF_COMPRESS_MEDIUM)],
        [InlineKeyboardButton(text="🔴 High (smallest size)", callback_data=PDF_COMPRESS_HIGH)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])


def rotate_angle_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↻ 90°", callback_data=PDF_ROTATE_90)],
        [InlineKeyboardButton(text="↻ 180°", callback_data=PDF_ROTATE_180)],
        [InlineKeyboardButton(text="↻ 270°", callback_data=PDF_ROTATE_270)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])


def pdf_to_images_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="PNG", callback_data=PDF_TO_IMG_PNG)],
        [InlineKeyboardButton(text="JPEG", callback_data=PDF_TO_IMG_JPEG)],
        [InlineKeyboardButton(text=const.BACK, callback_data=const.CB_BACK)],
        [InlineKeyboardButton(text=const.HOME, callback_data=const.CB_HOME)],
        [InlineKeyboardButton(text=const.CANCEL, callback_data=const.CB_CANCEL)],
    ])
