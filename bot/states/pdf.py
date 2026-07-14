from aiogram.fsm.state import State, StatesGroup


class PDFStates(StatesGroup):
    # Merge (multi-file upload + Done button)
    waiting_for_files_merge = State()

    # Split
    waiting_for_file_split = State()
    waiting_for_split_ranges = State()

    # Compress
    waiting_for_compress_level = State()
    waiting_for_file_compress = State()

    # Rotate
    waiting_for_rotate_angle = State()
    waiting_for_file_rotate = State()

    # Extract pages
    waiting_for_file_extract = State()
    waiting_for_extract_ranges = State()

    # Rearrange pages
    waiting_for_file_rearrange = State()
    waiting_for_rearrange_order = State()

    # Watermark
    waiting_for_watermark_text = State()
    waiting_for_file_watermark = State()

    # Add password
    waiting_for_password_add_value = State()
    waiting_for_file_password_add = State()

    # Remove password
    waiting_for_file_password_remove = State()
    waiting_for_password_remove_value = State()

    # Image to PDF (multi-file upload + Done button)
    waiting_for_images_to_pdf = State()

    # PDF to Images
    waiting_for_format_pdf_to_images = State()
    waiting_for_file_pdf_to_images = State()
