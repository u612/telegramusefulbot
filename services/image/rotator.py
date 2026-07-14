"""Rotate an image by a fixed angle."""
import asyncio

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.image._common import ImageProcessingError, open_image

VALID_ANGLES = (90, 180, 270)


class ImageRotator:
    async def rotate(self, input_path: str, angle: int) -> str:
        if angle not in VALID_ANGLES:
            raise ImageProcessingError(f"Angle must be one of {VALID_ANGLES} degrees.")
        return await asyncio.to_thread(self._rotate_sync, input_path, angle)

    @staticmethod
    def _rotate_sync(input_path: str, angle: int) -> str:
        img = open_image(input_path)
        try:
            # PIL rotates counter-clockwise; negate for the more intuitive
            # clockwise rotation users expect from a "rotate 90" button.
            rotated = img.rotate(-angle, expand=True)
            ext = f".{(img.format or 'PNG').lower()}"
            if ext == ".jpeg":
                ext = ".jpg"
            output_path = new_temp_path(suffix=ext)
            rotated.save(output_path)
            logger.info(f"Rotated {input_path} by {angle} degrees -> {output_path}")
            return output_path
        finally:
            img.close()
