"""Reorder every page of a PDF according to a user-specified permutation."""
import asyncio

from pypdf import PdfWriter

from core.logger import logger
from utils.tempfiles import new_temp_path
from utils.page_ranges import parse_rearrange_order
from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count


class PDFRearranger:
    async def rearrange(self, input_path: str, order_spec: str) -> str:
        return await asyncio.to_thread(self._rearrange_sync, input_path, order_spec)

    @staticmethod
    def _rearrange_sync(input_path: str, order_spec: str) -> str:
        reader = open_pdf_reader(input_path)
        total_pages = check_page_count(reader, min_pages=2)
        try:
            order = parse_rearrange_order(order_spec, total_pages)
        except ValueError as e:
            raise PDFProcessingError(str(e))

        writer = PdfWriter()
        try:
            for idx in order:
                writer.add_page(reader.pages[idx])

            output_path = new_temp_path(suffix=".pdf")
            with open(output_path, "wb") as f:
                writer.write(f)
            logger.info(f"Rearranged {input_path} using order {order_spec} -> {output_path}")
            return output_path
        finally:
            writer.close()
