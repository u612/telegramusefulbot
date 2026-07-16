"""Handlers for every Image Toolkit operation: Compress, Resize, Crop,
Rotate, Flip, Convert, Background Removal, Watermark, Remove Metadata.
"""
from typing import Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext

from bot.states.image import ImageStates
from bot.keyboards.image import (
    get_image_menu,
    IMG_COMPRESS, IMG_RESIZE, IMG_CROP, IMG_ROTATE, IMG_FLIP, IMG_CONVERT,
    IMG_BG_REMOVE, IMG_WATERMARK, IMG_METADATA_REMOVE,
    IMG_COMPRESS_LOW, IMG_COMPRESS_MEDIUM, IMG_COMPRESS_HIGH,
    IMG_ROTATE_90, IMG_ROTATE_180, IMG_ROTATE_270,
    IMG_FLIP_H, IMG_FLIP_V,
    IMG_CONVERT_JPEG, IMG_CONVERT_PNG, IMG_CONVERT_WEBP, IMG_CONVERT_BMP,
    compression_level_keyboard, rotate_angle_keyboard, flip_direction_keyboard,
    convert_format_keyboard,
)
from bot.keyboards.common import back_home_cancel
from core.constants import CB_IMAGE, SUPPORTED_IMAGE_EXTS
from core.logger import logger
from utils.limits import get_effective_limits

from services.image.compressor import ImageCompressor, CompressionLevel
from services.image.resizer import ImageResizer
from services.image.cropper import ImageCropper
from services.image.rotator import ImageRotator
from services.image.flipper import ImageFlipper
from services.image.converter import ImageConverter
from services.image.bg_remover import BackgroundRemover
from services.image.watermark import ImageWatermark
from services.image.metadata import ImageMetadataStripper
from services.image._common import ImageProcessingError

from utils.tempfiles import new_temp_path, track_temp_file, untrack_temp_files, delete_paths
from utils.validators import validate_extension, validate_upload

router = Router()

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif"}


@router.callback_query(F.data == CB_IMAGE)
async def image_menu_open(query: CallbackQuery, state: FSMContext):
    """Opens the Image submenu from the main menu's "🖼 Images" button."""
    await query.message.edit_text(
        "🖼 Image Toolkit -- choose an operation:",
        reply_markup=get_image_menu(),
    )
    await query.answer()


async def _track_usage(user_repo, db_user) -> None:
    if user_repo is not None and db_user is not None:
        try:
            await user_repo.increment_usage(db_user.telegram_id)
        except Exception:
            logger.exception("Failed to increment usage counter (non-fatal).")


async def _download_and_validate_image(message: Message, state: FSMContext, db_user=None) -> Optional[str]:
    doc = message.document
    if doc is None:
        if message.photo:
            await message.answer(
                "Please send the image as a file/document (not a compressed photo) "
                "to preserve quality -- use the paperclip/attach icon and pick 'File'."
            )
        else:
            await message.answer("Please send an image file as a document.")
        return None

    limits = get_effective_limits(message.from_user.id, db_user)
    if not limits.unlimited and doc.file_size and doc.file_size > limits.file_size:
        limit_mb = limits.file_size // (1024 * 1024)
        await message.answer(f"File too large (max {limit_mb} MB).")
        return None

    if not validate_extension(doc.file_name or "", SUPPORTED_IMAGE_EXTS):
        allowed_str = ", ".join(sorted(SUPPORTED_IMAGE_EXTS))
        await message.answer(f"Unsupported file type. Allowed: {allowed_str}")
        return None

    suffix = "." + (doc.file_name or "").rsplit(".", 1)[-1].lower() if "." in (doc.file_name or "") else ".jpg"
    temp_path = new_temp_path(suffix=suffix)
    await track_temp_file(state, temp_path)

    try:
        await message.bot.download(doc, destination=temp_path)
    except Exception as e:
        logger.error(f"Download failed: {e}")
        await untrack_temp_files(state, [temp_path])
        delete_paths([temp_path])
        await message.answer("Failed to download the file from Telegram. Please try again.")
        return None

    error = validate_upload(
        temp_path, doc.file_name or f"file{suffix}",
        allowed_extensions=SUPPORTED_IMAGE_EXTS,
        allowed_mime_types=_IMAGE_MIMES,
        max_size=limits.file_size,
    )
    if error:
        await untrack_temp_files(state, [temp_path])
        delete_paths([temp_path])
        await message.answer(error)
        return None

    return temp_path


async def _finish_with_image(message: Message, state: FSMContext, output_path: str,
                              filename: str, cleanup_paths: list, caption: Optional[str] = None) -> None:
    try:
        await message.answer_document(FSInputFile(output_path, filename=filename), caption=caption)
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()


async def _fail(message: Message, state: FSMContext, error: Exception, cleanup_paths: list) -> None:
    if isinstance(error, ImageProcessingError):
        await message.answer(f"⚠️ {error}")
    else:
        logger.exception(f"Unexpected image processing error: {error}")
        await message.answer("Something went wrong processing that image. Please try again.")
    delete_paths(cleanup_paths)
    await untrack_temp_files(state, cleanup_paths)
    await state.clear()


# --------------------------------------------------------------------------
# Compress
# --------------------------------------------------------------------------

@router.callback_query(F.data == IMG_COMPRESS)
async def img_compress_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ImageStates.waiting_for_compress_level)
    await query.message.edit_text("Choose a compression level:", reply_markup=compression_level_keyboard())
    await query.answer()


@router.callback_query(
    ImageStates.waiting_for_compress_level,
    F.data.in_({IMG_COMPRESS_LOW, IMG_COMPRESS_MEDIUM, IMG_COMPRESS_HIGH}),
)
async def img_compress_level_chosen(query: CallbackQuery, state: FSMContext):
    level_map = {
        IMG_COMPRESS_LOW: CompressionLevel.LOW,
        IMG_COMPRESS_MEDIUM: CompressionLevel.MEDIUM,
        IMG_COMPRESS_HIGH: CompressionLevel.HIGH,
    }
    await state.update_data(compress_level=level_map[query.data].value)
    await state.set_state(ImageStates.waiting_for_file_compress)
    await query.message.edit_text("Send the image to compress.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(ImageStates.waiting_for_file_compress, F.document)
async def img_compress_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate_image(message, state, db_user=db_user)
    if path is None:
        return
    data = await state.get_data()
    level = CompressionLevel(data.get("compress_level", CompressionLevel.MEDIUM.value))

    await message.answer("Compressing... please wait.")
    try:
        output_path = await ImageCompressor().compress(path, level)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    filename = "compressed" + output_path[output_path.rfind("."):]
    await _finish_with_image(message, state, output_path, filename, [path, output_path])


# --------------------------------------------------------------------------
# Resize
# --------------------------------------------------------------------------

@router.callback_query(F.data == IMG_RESIZE)
async def img_resize_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ImageStates.waiting_for_file_resize)
    await query.message.edit_text("Send the image to resize.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(ImageStates.waiting_for_file_resize, F.document)
async def img_resize_receive(message: Message, state: FSMContext, db_user=None):
    path = await _download_and_validate_image(message, state, db_user=db_user)
    if path is None:
        return
    await state.update_data(resize_input_path=path)
    await state.set_state(ImageStates.waiting_for_resize_dims)
    await message.answer(
        "Send the target size, e.g. '800x600' for an exact size, or just '800' to scale "
        "proportionally by width.",
        reply_markup=back_home_cancel(),
    )


@router.message(ImageStates.waiting_for_resize_dims, F.text)
async def img_resize_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("resize_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    await message.answer("Resizing... please wait.")
    try:
        output_path = await ImageResizer().resize(path, message.text.strip())
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    filename = "resized" + output_path[output_path.rfind("."):]
    await _finish_with_image(message, state, output_path, filename, [path, output_path])


# --------------------------------------------------------------------------
# Crop
# --------------------------------------------------------------------------

@router.callback_query(F.data == IMG_CROP)
async def img_crop_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ImageStates.waiting_for_file_crop)
    await query.message.edit_text("Send the image to crop.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(ImageStates.waiting_for_file_crop, F.document)
async def img_crop_receive(message: Message, state: FSMContext, db_user=None):
    path = await _download_and_validate_image(message, state, db_user=db_user)
    if path is None:
        return
    await state.update_data(crop_input_path=path)
    await state.set_state(ImageStates.waiting_for_crop_box)
    await message.answer(
        "Send the crop box in pixels as 'x1,y1,x2,y2' (top-left to bottom-right), e.g. 0,0,500,500",
        reply_markup=back_home_cancel(),
    )


@router.message(ImageStates.waiting_for_crop_box, F.text)
async def img_crop_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("crop_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    await message.answer("Cropping... please wait.")
    try:
        output_path = await ImageCropper().crop(path, message.text.strip())
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    filename = "cropped" + output_path[output_path.rfind("."):]
    await _finish_with_image(message, state, output_path, filename, [path, output_path])


# --------------------------------------------------------------------------
# Rotate
# --------------------------------------------------------------------------

@router.callback_query(F.data == IMG_ROTATE)
async def img_rotate_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ImageStates.waiting_for_rotate_angle)
    await query.message.edit_text("Choose a rotation angle:", reply_markup=rotate_angle_keyboard())
    await query.answer()


@router.callback_query(
    ImageStates.waiting_for_rotate_angle,
    F.data.in_({IMG_ROTATE_90, IMG_ROTATE_180, IMG_ROTATE_270}),
)
async def img_rotate_angle_chosen(query: CallbackQuery, state: FSMContext):
    angle_map = {IMG_ROTATE_90: 90, IMG_ROTATE_180: 180, IMG_ROTATE_270: 270}
    await state.update_data(rotate_angle=angle_map[query.data])
    await state.set_state(ImageStates.waiting_for_file_rotate)
    await query.message.edit_text("Send the image to rotate.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(ImageStates.waiting_for_file_rotate, F.document)
async def img_rotate_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate_image(message, state, db_user=db_user)
    if path is None:
        return
    data = await state.get_data()
    angle = data.get("rotate_angle", 90)

    await message.answer("Rotating... please wait.")
    try:
        output_path = await ImageRotator().rotate(path, angle)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    filename = "rotated" + output_path[output_path.rfind("."):]
    await _finish_with_image(message, state, output_path, filename, [path, output_path])


# --------------------------------------------------------------------------
# Flip
# --------------------------------------------------------------------------

@router.callback_query(F.data == IMG_FLIP)
async def img_flip_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ImageStates.waiting_for_flip_direction)
    await query.message.edit_text("Choose a flip direction:", reply_markup=flip_direction_keyboard())
    await query.answer()


@router.callback_query(ImageStates.waiting_for_flip_direction, F.data.in_({IMG_FLIP_H, IMG_FLIP_V}))
async def img_flip_direction_chosen(query: CallbackQuery, state: FSMContext):
    direction = "horizontal" if query.data == IMG_FLIP_H else "vertical"
    await state.update_data(flip_direction=direction)
    await state.set_state(ImageStates.waiting_for_file_flip)
    await query.message.edit_text("Send the image to flip.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(ImageStates.waiting_for_file_flip, F.document)
async def img_flip_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate_image(message, state, db_user=db_user)
    if path is None:
        return
    data = await state.get_data()
    direction = data.get("flip_direction", "horizontal")

    await message.answer("Flipping... please wait.")
    try:
        output_path = await ImageFlipper().flip(path, direction)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    filename = "flipped" + output_path[output_path.rfind("."):]
    await _finish_with_image(message, state, output_path, filename, [path, output_path])


# --------------------------------------------------------------------------
# Convert format
# --------------------------------------------------------------------------

@router.callback_query(F.data == IMG_CONVERT)
async def img_convert_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ImageStates.waiting_for_convert_format)
    await query.message.edit_text("Choose the target format:", reply_markup=convert_format_keyboard())
    await query.answer()


@router.callback_query(
    ImageStates.waiting_for_convert_format,
    F.data.in_({IMG_CONVERT_JPEG, IMG_CONVERT_PNG, IMG_CONVERT_WEBP, IMG_CONVERT_BMP}),
)
async def img_convert_format_chosen(query: CallbackQuery, state: FSMContext):
    fmt_map = {
        IMG_CONVERT_JPEG: "jpeg", IMG_CONVERT_PNG: "png",
        IMG_CONVERT_WEBP: "webp", IMG_CONVERT_BMP: "bmp",
    }
    await state.update_data(convert_format=fmt_map[query.data])
    await state.set_state(ImageStates.waiting_for_file_convert)
    await query.message.edit_text("Send the image to convert.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(ImageStates.waiting_for_file_convert, F.document)
async def img_convert_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate_image(message, state, db_user=db_user)
    if path is None:
        return
    data = await state.get_data()
    fmt = data.get("convert_format", "png")

    await message.answer("Converting... please wait.")
    try:
        output_path = await ImageConverter().convert(path, fmt)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    filename = "converted" + output_path[output_path.rfind("."):]
    await _finish_with_image(message, state, output_path, filename, [path, output_path])


# --------------------------------------------------------------------------
# Background removal
# --------------------------------------------------------------------------

@router.callback_query(F.data == IMG_BG_REMOVE)
async def img_bg_remove_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ImageStates.waiting_for_file_bg_remove)
    await query.message.edit_text(
        "Send the image to remove the background from.\n"
        "This can take up to a minute on the first request.",
        reply_markup=back_home_cancel(),
    )
    await query.answer()


@router.message(ImageStates.waiting_for_file_bg_remove, F.document)
async def img_bg_remove_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate_image(message, state, db_user=db_user)
    if path is None:
        return

    await message.answer("Removing background... this may take a while, please wait.")
    try:
        output_path = await BackgroundRemover().remove_background(path)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    await _finish_with_image(message, state, output_path, "no_background.png", [path, output_path])


# --------------------------------------------------------------------------
# Watermark
# --------------------------------------------------------------------------

@router.callback_query(F.data == IMG_WATERMARK)
async def img_watermark_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ImageStates.waiting_for_file_watermark)
    await query.message.edit_text("Send the image to watermark.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(ImageStates.waiting_for_file_watermark, F.document)
async def img_watermark_receive(message: Message, state: FSMContext, db_user=None):
    path = await _download_and_validate_image(message, state, db_user=db_user)
    if path is None:
        return
    await state.update_data(watermark_input_path=path)
    await state.set_state(ImageStates.waiting_for_watermark_text)
    await message.answer("Send the watermark text (max 100 characters).", reply_markup=back_home_cancel())


@router.message(ImageStates.waiting_for_watermark_text, F.text)
async def img_watermark_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("watermark_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    await message.answer("Applying watermark... please wait.")
    try:
        output_path = await ImageWatermark().add_watermark(path, message.text)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    filename = "watermarked" + output_path[output_path.rfind("."):]
    await _finish_with_image(message, state, output_path, filename, [path, output_path])


# --------------------------------------------------------------------------
# Metadata removal
# --------------------------------------------------------------------------

@router.callback_query(F.data == IMG_METADATA_REMOVE)
async def img_metadata_remove_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ImageStates.waiting_for_file_metadata_remove)
    await query.message.edit_text("Send the image to strip metadata from.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(ImageStates.waiting_for_file_metadata_remove, F.document)
async def img_metadata_remove_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate_image(message, state, db_user=db_user)
    if path is None:
        return

    await message.answer("Removing metadata... please wait.")
    try:
        output_path = await ImageMetadataStripper().strip(path)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    filename = "clean" + output_path[output_path.rfind("."):]
    await _finish_with_image(message, state, output_path, filename, [path, output_path])
