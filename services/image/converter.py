"""Convert an image between formats."""
import asyncio

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.image._common import ImageProcessingError, open_image, flatten_to_rgb

ALLOWED_FORMATS = {"jpeg", "png", "webp", "bmp"}
_NO_ALPHA_FORMATS = {"jpeg", "bmp"}


class ImageConverter:
    async def convert(self, input_path: str, target_format: str) -> str:
        target_format = target_format.lower()
        if target_format not in ALLOWED_FORMATS:
            raise ImageProcessingError(f"Format must be one of {sorted(ALLOWED_FORMATS)}.")
        return await asyncio.to_thread(self._convert_sync, input_path, target_format)

    @staticmethod
    def _convert_sync(input_path: str, target_format: str) -> str:
        img = open_image(input_path)
        try:
            to_save = flatten_to_rgb(img) if target_format in _NO_ALPHA_FORMATS else img
            ext = ".jpg" if target_format == "jpeg" else f".{target_format}"
            output_path = new_temp_path(suffix=ext)
            pillow_format = "JPEG" if target_format == "jpeg" else target_format.upper()
            to_save.save(output_path, pillow_format)
            logger.info(f"Converted {input_path} to {target_format} -> {output_path}")
            return output_path
        finally:
            img.close()
