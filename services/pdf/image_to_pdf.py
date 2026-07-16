"""Combine one or more images into a single multi-page PDF."""
import asyncio
from typing import List

from PIL import Image, UnidentifiedImageError

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.pdf._common import PDFProcessingError
from typing import Optional


def _load_as_rgb(path: str) -> Image.Image:
    try:
        img = Image.open(path)
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise PDFProcessingError(f"Could not read image file: {e}")

    if img.mode in ("RGBA", "LA", "P"):
        # The PDF plugin needs RGB/L; flatten any transparency onto white
        # so we don't silently lose it or crash on save.
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


class ImageToPDF:
    async def convert(self, image_paths: List[str], max_images: Optional[int] = None) -> str:
        """`max_images` should come from the caller's effective limits
        (utils.limits) -- pass None (or omit) for no cap, i.e. the owner.
        """
        if not image_paths:
            raise PDFProcessingError("No images provided.")
        if max_images is not None and len(image_paths) > max_images:
            raise PDFProcessingError(f"Too many images (max {max_images}).")
        return await asyncio.to_thread(self._convert_sync, image_paths)

    @staticmethod
    def _convert_sync(image_paths: List[str]) -> str:
        images = [_load_as_rgb(p) for p in image_paths]
        try:
            output_path = new_temp_path(suffix=".pdf")
            first, rest = images[0], images[1:]
            first.save(output_path, "PDF", save_all=True, append_images=rest)
            logger.info(f"Converted {len(images)} image(s) to PDF -> {output_path}")
            return output_path
        finally:
            for img in images:
                img.close()
