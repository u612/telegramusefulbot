"""Rasterize selected pages of a PDF into image files (PNG or JPEG).

`get_page_count` is a lightweight, fast open used purely for validation /
showing the page-selection screen. `convert` does the actual rendering and
only ever touches the pages it's asked for -- callers pass 1-indexed page
numbers and get back `(page_number, path)` pairs in the same order, so the
original page numbering survives into filenames even for a custom range
(see PDF -> Images' filename helper in bot/handlers/pdf/image.py).
"""
import asyncio
from typing import List, Optional, Tuple

import fitz  # PyMuPDF

from core.logger import logger
from utils.tempfiles import new_temp_path, delete_paths
from services.pdf._common import PDFProcessingError

# PDF -> Images is used interactively (the user waits for it), and each
# selected page becomes a full-resolution raster -- so the cap is higher
# than incidental/administrative PDF ceilings elsewhere, but still bounded
# to avoid a pathological document hanging processing or exhausting disk.
MAX_PAGES_TO_RASTER = 300

DEFAULT_DPI = 150
ALLOWED_FORMATS = {"png", "jpeg"}

# Named quality presets surfaced in the PDF -> Images quality-selection
# screen. Keep these in sync with bot/handlers/pdf/image.py's keyboard.
DPI_STANDARD = 150
DPI_HIGH = 300
DPI_MAXIMUM = 600


class PDFToImages:
    async def get_page_count(self, input_path: str) -> int:
        """Open the PDF just far enough to report its page count, raising
        PDFProcessingError (with a user-facing message) if it can't be
        opened, is password protected, or has zero pages. Does not
        rasterize anything.
        """
        return await asyncio.to_thread(self._get_page_count_sync, input_path)

    @staticmethod
    def _get_page_count_sync(input_path: str) -> int:
        try:
            doc = fitz.open(input_path)
        except Exception as e:
            raise PDFProcessingError(
                "Unable to read this PDF. The file may be damaged or corrupted."
            ) from e

        try:
            if doc.needs_pass:
                raise PDFProcessingError(
                    "This PDF is password protected. Please remove the password "
                    "before converting it to images."
                )
            page_count = doc.page_count
            if page_count == 0:
                raise PDFProcessingError("This PDF contains no pages.")
            return page_count
        finally:
            doc.close()

    async def convert(
        self,
        input_path: str,
        image_format: str = "png",
        dpi: int = DEFAULT_DPI,
        pages: Optional[List[int]] = None,
    ) -> List[Tuple[int, str]]:
        """Rasterize `pages` (1-indexed page numbers) at the given DPI, or
        every page if `pages` is None/empty. Returns a list of
        `(page_number, path)` tuples, in the same order as `pages`.
        """
        image_format = image_format.lower()
        if image_format not in ALLOWED_FORMATS:
            raise PDFProcessingError(f"Format must be one of {sorted(ALLOWED_FORMATS)}.")
        return await asyncio.to_thread(self._convert_sync, input_path, image_format, dpi, pages)

    @staticmethod
    def _convert_sync(
        input_path: str, image_format: str, dpi: int, pages: Optional[List[int]]
    ) -> List[Tuple[int, str]]:
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

            target_pages = list(pages) if pages else list(range(1, page_count + 1))
            if any(p < 1 or p > page_count for p in target_pages):
                raise PDFProcessingError(f"Some pages don't exist. This PDF has {page_count} pages.")
            if len(target_pages) > MAX_PAGES_TO_RASTER:
                raise PDFProcessingError(
                    f"This selection has {len(target_pages)} pages; this feature supports up to "
                    f"{MAX_PAGES_TO_RASTER} pages at a time."
                )

            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            ext = "jpg" if image_format == "jpeg" else "png"

            output: List[Tuple[int, str]] = []
            try:
                for page_num in target_pages:
                    page = doc.load_page(page_num - 1)
                    pix = page.get_pixmap(matrix=matrix, alpha=(image_format == "png"))
                    out_path = new_temp_path(suffix=f".{ext}")
                    pix.save(out_path)
                    output.append((page_num, out_path))

                logger.info(f"Rasterized {len(output)} page(s) from {input_path} -> images")
                return output
            except Exception:
                delete_paths([p for _, p in output])
                raise
        finally:
            doc.close()
