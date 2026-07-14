from aiogram.fsm.state import State, StatesGroup


class ArchiveStates(StatesGroup):
    # Compress (choose format, then multi-file upload + Done)
    waiting_for_compress_format = State()
    waiting_for_files_compress = State()

    # Extract (single archive upload)
    waiting_for_file_extract = State()
