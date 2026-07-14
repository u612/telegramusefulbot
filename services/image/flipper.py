"""Flip an image horizontally or vertically."""
import asyncio

from PIL import Image as PILImage

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.image._common import ImageProcessingError, open_image

VALID_DIRECTIONS = ("horizontal", "vertical")


class ImageFlipper:
    async def flip(self, input_path: str, direction: str) -> str:
        if direction not in VALID_DIRECTIONS:
            raise ImageProcessingError(f"Direction must be one of {VALID_DIRECTIONS}.")
        return await asyncio.to_thread(self._flip_sync, input_path, direction)

    @staticmethod
    def _flip_sync(input_path: str, direction: str) -> str:
        img = open_image(input_path)
        try:
            method = PILImage.FLIP_LEFT_RIGHT if direction == "horizontal" else PILImage.FLIP_TOP_BOTTOM
            flipped = img.transpose(method)
            ext = f".{(img.format or 'PNG').lower()}"
            if ext == ".jpeg":
                ext = ".jpg"
            output_path = new_temp_path(suffix=ext)
            flipped.save(output_path)
            logger.info(f"Flipped {input_path} ({direction}) -> {output_path}")
            return output_path
        finally:
            img.close()
