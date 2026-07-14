"""Crop an image to a pixel bounding box."""
import asyncio
import re

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.image._common import ImageProcessingError, open_image

_BOX_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")


class ImageCropper:
    async def crop(self, input_path: str, box_spec: str) -> str:
        return await asyncio.to_thread(self._crop_sync, input_path, box_spec)

    @staticmethod
    def _crop_sync(input_path: str, box_spec: str) -> str:
        img = open_image(input_path)
        try:
            m = _BOX_RE.match(box_spec)
            if not m:
                raise ImageProcessingError("Send the crop box as 'x1,y1,x2,y2' in pixels, e.g. 0,0,500,500")
            x1, y1, x2, y2 = (int(g) for g in m.groups())
            width, height = img.size
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                raise ImageProcessingError(
                    f"Crop box must be within the image ({width}x{height}) and x1<x2, y1<y2."
                )

            cropped = img.crop((x1, y1, x2, y2))
            ext = f".{(img.format or 'PNG').lower()}"
            if ext == ".jpeg":
                ext = ".jpg"
            output_path = new_temp_path(suffix=ext)
            cropped.save(output_path)
            logger.info(f"Cropped {input_path} to {(x1, y1, x2, y2)} -> {output_path}")
            return output_path
        finally:
            img.close()
