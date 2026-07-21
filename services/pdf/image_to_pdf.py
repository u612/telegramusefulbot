"""Combine one or more images into a single multi-page PDF."""
import asyncio
from typing import List

from PIL import Image, UnidentifiedImageError

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.pdf._common import PDFProcessingError
from typing import Optional

# Page sizes in points (1 point = 1px at the 72 DPI resolution we save
# with below, so these pixel dimensions map to exact physical page sizes).
_PAGE_SIZES_PT = {
    "A4": (595, 842),
    "LETTER": (612, 792),
}


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


def _fit_to_page(img: Image.Image, page_size: tuple) -> Image.Image:
    """Return a new RGB image the exact size of `page_size` (px), with
    `img` scaled down (never up) to fit inside it, aspect ratio preserved,
    centered on a white background. Never stretches.
    """
    page_w, page_h = page_size
    src_w, src_h = img.size
    scale = min(page_w / src_w, page_h / src_h, 1.0)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS) if (new_w, new_h) != (src_w, src_h) else img
    canvas = Image.new("RGB", (page_w, page_h), (255, 255, 255))
    offset = ((page_w - new_w) // 2, (page_h - new_h) // 2)
    canvas.paste(resized, offset)
    if resized is not img:
        resized.close()
    return canvas


class ImageToPDF:
    async def convert(
        self,
        image_paths: List[str],
        max_images: Optional[int] = None,
        page_size: Optional[str] = None,
    ) -> str:
        """`max_images` should come from the caller's effective limits
        (utils.limits) -- pass None (or omit) for no cap, i.e. the owner.

        `page_size` is one of "A4", "LETTER", or None/"ORIGINAL" -- when
        None, each page matches its source image's own dimensions
        (no resizing).
        """
        if not image_paths:
            raise PDFProcessingError("No images provided.")
        if max_images is not None and len(image_paths) > max_images:
            raise PDFProcessingError(f"Too many images (max {max_images}).")
        return await asyncio.to_thread(self._convert_sync, image_paths, page_size)

    @staticmethod
    def _convert_sync(image_paths: List[str], page_size: Optional[str] = None) -> str:
        images = [_load_as_rgb(p) for p in image_paths]
        pages = images
        target = _PAGE_SIZES_PT.get((page_size or "").upper())
        try:
            if target is not None:
                pages = [_fit_to_page(img, target) for img in images]
            output_path = new_temp_path(suffix=".pdf")
            first, rest = pages[0], pages[1:]
            first.save(output_path, "PDF", save_all=True, append_images=rest, resolution=72.0)
            logger.info(
                f"Converted {len(images)} image(s) to PDF (page_size={page_size or 'original'}) -> {output_path}"
            )
            return output_path
        finally:
            for img in images:
                img.close()
            if pages is not images:
                for pg in pages:
                    pg.close()
