from aiogram.fsm.state import State, StatesGroup


class ImageStates(StatesGroup):
    # Compress
    waiting_for_compress_level = State()
    waiting_for_file_compress = State()

    # Resize
    waiting_for_file_resize = State()
    waiting_for_resize_dims = State()

    # Crop
    waiting_for_file_crop = State()
    waiting_for_crop_box = State()

    # Rotate
    waiting_for_rotate_angle = State()
    waiting_for_file_rotate = State()

    # Flip
    waiting_for_flip_direction = State()
    waiting_for_file_flip = State()

    # Convert format
    waiting_for_convert_format = State()
    waiting_for_file_convert = State()

    # Background removal
    waiting_for_file_bg_remove = State()

    # Watermark
    waiting_for_file_watermark = State()
    waiting_for_watermark_text = State()

    # Metadata removal
    waiting_for_file_metadata_remove = State()
