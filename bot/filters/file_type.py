from aiogram.filters import BaseFilter
from aiogram.types import Message
from typing import Union, Optional


class FileTypeFilter(BaseFilter):
    """Filter messages by file type (document, photo, etc.)."""

    def __init__(self, file_type: str):
        self.file_type = file_type  # 'document', 'photo', etc.

    async def __call__(self, message: Message) -> bool:
        if self.file_type == "document" and message.document:
            return True
        if self.file_type == "photo" and message.photo:
            return True
        # Add others if needed
        return False
