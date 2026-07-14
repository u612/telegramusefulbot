"""Rotate every page of a PDF by a fixed angle."""
import asyncio

from pypdf import PdfWriter

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.pdf._common import PDFProcessingError, open_pdf_reader

VALID_ANGLES = (90, 180, 270)


class PDFRotator:
    async def rotate(self, input_path: str, angle: int) -> str:
        if angle not in VALID_ANGLES:
            raise PDFProcessingError(f"Angle must be one of {VALID_ANGLES} degrees.")
        return await asyncio.to_thread(self._rotate_sync, input_path, angle)

    @staticmethod
    def _rotate_sync(input_path: str, angle: int) -> str:
        reader = open_pdf_reader(input_path)
        writer = PdfWriter()
        try:
            for page in reader.pages:
                page.rotate(angle)
                writer.add_page(page)

            output_path = new_temp_path(suffix=".pdf")
            with open(output_path, "wb") as f:
                writer.write(f)
            logger.info(f"Rotated {input_path} by {angle} degrees -> {output_path}")
            return output_path
        finally:
            writer.close()
          
