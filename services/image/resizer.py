"""Resize an image, either to an exact WxH or proportionally by width."""
import asyncio
import re
from typing import Tuple

from PIL import Image as PILImage

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.image._common import ImageProcessingError, open_image

MAX_DIMENSION = 10000

_EXACT_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")
_WIDTH_ONLY_RE = re.compile(r"^\s*(\d+)\s*$")


def parse_resize_spec(spec: str, original_size: Tuple[int, int]) -> Tuple[int, int]:
    """'800x600' -> exact size. '800' -> width 800, height scaled to preserve
    aspect ratio.
    """
    m = _EXACT_RE.match(spec)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    else:
        m = _WIDTH_ONLY_RE.match(spec)
        if not m:
            raise ImageProcessingError("Send a size like '800x600' or just '800' for proportional width.")
        w = int(m.group(1))
        orig_w, orig_h = original_size
        h = max(1, round(orig_h * (w / orig_w)))

    if w < 1 or h < 1:
        raise ImageProcessingError("Width and height must be positive.")
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        raise ImageProcessingError(f"Dimensions too large (max {MAX_DIMENSION}px per side).")
    return w, h


class ImageResizer:
    async def resize(self, input_path: str, spec: str) -> str:
        return await asyncio.to_thread(self._resize_sync, input_path, spec)

    @staticmethod
    def _resize_sync(input_path: str, spec: str) -> str:
        img = open_image(input_path)
        try:
            target = parse_resize_spec(spec, img.size)
            resized = img.resize(target, resample=PILImage.LANCZOS)
            ext = f".{(img.format or 'PNG').lower()}"
            if ext == ".jpeg":
                ext = ".jpg"
            output_path = new_temp_path(suffix=ext)
            resized.save(output_path)
            logger.info(f"Resized {input_path} to {target} -> {output_path}")
            return output_path
        finally:
            img.close()
