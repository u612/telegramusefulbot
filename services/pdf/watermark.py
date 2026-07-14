"""Stamp a diagonal, semi-transparent text watermark onto every page."""
import asyncio
import io

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.pdf._common import PDFProcessingError, open_pdf_reader

MAX_WATERMARK_TEXT_LEN = 100


def _build_watermark_overlay(width: float, height: float, text: str,
                              opacity: float = 0.25, angle: float = 45,
                              font_size: int = 40) -> "PdfReader":
    """Render a single-page overlay sized to match the target page, with
    the text centered and rotated diagonally.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.saveState()
    c.setFillColor(Color(0.5, 0.5, 0.5, alpha=opacity))
    c.setFont("Helvetica-Bold", font_size)
    c.translate(width / 2, height / 2)
    c.rotate(angle)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    c.save()
    buf.seek(0)
    return PdfReader(buf)


class PDFWatermark:
    async def add_watermark(self, input_path: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise PDFProcessingError("Watermark text can't be empty.")
        if len(text) > MAX_WATERMARK_TEXT_LEN:
            raise PDFProcessingError(f"Watermark text is too long (max {MAX_WATERMARK_TEXT_LEN} characters).")
        return await asyncio.to_thread(self._watermark_sync, input_path, text)

    @staticmethod
    def _watermark_sync(input_path: str, text: str) -> str:
        reader = open_pdf_reader(input_path)
        writer = PdfWriter()
        try:
            for page in reader.pages:
                box = page.mediabox
                width, height = float(box.width), float(box.height)
                overlay_reader = _build_watermark_overlay(width, height, text)
                page.merge_page(overlay_reader.pages[0])
                writer.add_page(page)

            output_path = new_temp_path(suffix=".pdf")
            with open(output_path, "wb") as f:
                writer.write(f)
            logger.info(f"Watermarked {input_path} -> {output_path}")
            return output_path
        finally:
            writer.close()
