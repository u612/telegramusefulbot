"""Rotate pages of a PDF by a fixed angle -- either every page, or just a
specific subset (0-indexed page numbers).
"""
import asyncio
from typing import List, Optional

from pypdf import PdfWriter

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.pdf._common import PDFProcessingError, open_pdf_reader

VALID_ANGLES = (90, 180, 270)


class PDFRotator:
    async def rotate(self, input_path: str, angle: int, pages: Optional[List[int]] = None) -> str:
        """Rotate `input_path` by `angle` degrees (clockwise; one of
        VALID_ANGLES). If `pages` is None (default -- unchanged from the
        original single-mode behavior), every page is rotated. Otherwise
        `pages` is a list of 0-indexed page numbers and only those pages
        are rotated; all others are copied through untouched.
        """
        if angle not in VALID_ANGLES:
            raise PDFProcessingError(f"Angle must be one of {VALID_ANGLES} degrees.")
        return await asyncio.to_thread(self._rotate_sync, input_path, angle, pages)

    @staticmethod
    def _rotate_sync(input_path: str, angle: int, pages: Optional[List[int]]) -> str:
        reader = open_pdf_reader(input_path)
        writer = PdfWriter()
        try:
            total_pages = len(reader.pages)
            targets = set(pages) if pages is not None else None
            if targets is not None:
                out_of_range = [p for p in targets if p < 0 or p >= total_pages]
                if out_of_range:
                    raise PDFProcessingError(
                        f"Page selection is outside the document ({total_pages} pages)."
                    )

            for i, page in enumerate(reader.pages):
                if targets is None or i in targets:
                    page.rotate(angle)
                writer.add_page(page)

            output_path = new_temp_path(suffix=".pdf")
            with open(output_path, "wb") as f:
                writer.write(f)
            logger.info(
                f"Rotated {input_path} by {angle} degrees "
                f"({'all pages' if targets is None else f'{len(targets)} page(s)'}) -> {output_path}"
            )
            return output_path
        finally:
            writer.close()
