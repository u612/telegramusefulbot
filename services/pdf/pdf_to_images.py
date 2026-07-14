"""Rasterize each page of a PDF into an image file (PNG or JPEG)."""
import asyncio
from typing import List

import fitz  # PyMuPDF

from core.logger import logger
from utils.tempfiles import new_temp_path, delete_paths
from services.pdf._common import PDFProcessingError

MAX_PAGES_TO_RASTER = 100
DEFAULT_DPI = 150
ALLOWED_FORMATS = {"png", "jpeg"}


class PDFToImages:
    async def convert(self, input_path: str, image_format: str = "png", dpi: int = DEFAULT_DPI) -> List[str]:
        image_format = image_format.lower()
        if image_format not in ALLOWED_FORMATS:
            raise PDFProcessingError(f"Format must be one of {sorted(ALLOWED_FORMATS)}.")
        return await asyncio.to_thread(self._convert_sync, input_path, image_format, dpi)

    @staticmethod
    def _convert_sync(input_path: str, image_format: str, dpi: int) -> List[str]:
        try:
            doc = fitz.open(input_path)
        except Exception as e:
            raise PDFProcessingError(f"Could not read this PDF (corrupted or unsupported format): {e}")

        try:
            if doc.needs_pass:
                raise PDFProcessingError(
                    "This PDF is password-protected. Use 'Remove Password' first, then retry."
                )

            page_count = doc.page_count
            if page_count == 0:
                raise PDFProcessingError("PDF has no pages.")
            if page_count > MAX_PAGES_TO_RASTER:
                raise PDFProcessingError(
                    f"PDF has {page_count} pages; this feature supports up to "
                    f"{MAX_PAGES_TO_RASTER} pages at a time."
                )

            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            ext = "jpg" if image_format == "jpeg" else "png"

            output_paths: List[str] = []
            try:
                for page_index in range(page_count):
                    page = doc.load_page(page_index)
                    pix = page.get_pixmap(matrix=matrix, alpha=(image_format == "png"))
                    out_path = new_temp_path(suffix=f".{ext}")
                    pix.save(out_path)
                    output_paths.append(out_path)

                logger.info(f"Rasterized {page_count} page(s) from {input_path} -> {len(output_paths)} images")
                return output_paths
            except Exception:
                delete_paths(output_paths)
                raise
        finally:
            doc.close()
