"""Strip EXIF/metadata from an image."""
import asyncio

from PIL import Image as PILImage

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.image._common import open_image


class ImageMetadataStripper:
    async def strip(self, input_path: str) -> str:
        return await asyncio.to_thread(self._strip_sync, input_path)

    @staticmethod
    def _strip_sync(input_path: str) -> str:
        img = open_image(input_path)
        try:
            # Rebuilding the image from raw pixel data (rather than just
            # calling save()) guarantees no EXIF/ICC/XMP metadata survives,
            # since Pillow only omits info-dict metadata this way, not by
            # default on a plain save of some formats.
            clean = PILImage.new(img.mode, img.size)
            clean.putdata(list(img.getdata()))

            ext = f".{(img.format or 'PNG').lower()}"
            if ext == ".jpeg":
                ext = ".jpg"
            output_path = new_temp_path(suffix=ext)
            clean.save(output_path, (img.format or "PNG"))
            logger.info(f"Stripped metadata from {input_path} -> {output_path}")
            return output_path
        finally:
            img.close()
