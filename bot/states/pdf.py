from aiogram.fsm.state import State, StatesGroup


class PDFStates(StatesGroup):
    # Merge (multi-file upload + Done button)
    waiting_for_files_merge = State()
    waiting_for_merge_arrange = State()
    waiting_for_merge_preview = State()
    waiting_for_merge_filename = State()

    # Split (redesigned to match Merge's UX: PDF -> choose method -> input
    # -> preview -> confirm -> process/upload)
    waiting_for_file_split = State()
    waiting_for_split_method = State()
    waiting_for_split_range_input = State()
    waiting_for_split_range_preview = State()
    waiting_for_split_extract_input = State()
    waiting_for_split_extract_preview = State()
    waiting_for_split_every_preview = State()
    waiting_for_split_large_confirm = State()

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
