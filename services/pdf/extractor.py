"""Extract a chosen set of pages from a PDF into a single new PDF."""
import asyncio

from pypdf import PdfWriter

from core.logger import logger
from utils.tempfiles import new_temp_path
from utils.page_ranges import parse_page_range_group
from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count


class PDFExtractor:
    async def extract(self, input_path: str, ranges_spec: str) -> str:
        return await asyncio.to_thread(self._extract_sync, input_path, ranges_spec)

    @staticmethod
    def _extract_sync(input_path: str, ranges_spec: str) -> str:
        reader = open_pdf_reader(input_path)
        total_pages = check_page_count(reader)
        try:
            page_indices = parse_page_range_group(ranges_spec, total_pages)
        except ValueError as e:
            raise PDFProcessingError(str(e))

        writer = PdfWriter()
        try:
            for idx in page_indices:
                writer.add_page(reader.pages[idx])

            output_path = new_temp_path(suffix=".pdf")
            with open(output_path, "wb") as f:
                writer.write(f)
            logger.info(f"Extracted {len(page_indices)} page(s) from {input_path} -> {output_path}")
            return output_path
        finally:
            writer.close()
