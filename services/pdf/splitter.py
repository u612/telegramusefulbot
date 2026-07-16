"""Split a PDF into multiple output PDFs, either one file per page or by
user-specified page-range groups.
"""
import asyncio
from typing import List, Optional

from pypdf import PdfWriter

from core.logger import logger
from utils.tempfiles import new_temp_path
from utils.page_ranges import parse_multi_group_ranges
from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count


class PDFSplitter:
    async def split(
        self, input_path: str, ranges_spec: Optional[str] = None, max_groups: Optional[int] = None
    ) -> List[str]:
        """If `ranges_spec` is None, splits into one PDF per page. Otherwise
        `ranges_spec` is a ';'-separated list of page-range groups (e.g.
        '1-3;4-6;7'), and each group becomes one output PDF.
        `max_groups` should come from the caller's effective limits
        (utils.limits) -- pass None (or omit) for no cap, i.e. the owner.
        Returns a list of output file paths, in order.
        """
        return await asyncio.to_thread(self._split_sync, input_path, ranges_spec, max_groups)

    @staticmethod
    def _split_sync(input_path: str, ranges_spec: Optional[str], max_groups: Optional[int]) -> List[str]:
        reader = open_pdf_reader(input_path)
        total_pages = check_page_count(reader, min_pages=2)

        if ranges_spec:
            try:
                groups = parse_multi_group_ranges(ranges_spec, total_pages)
            except ValueError as e:
                raise PDFProcessingError(str(e))
        else:
            groups = [[i] for i in range(total_pages)]

        if max_groups is not None and len(groups) > max_groups:
            raise PDFProcessingError(f"Too many split groups requested (max {max_groups}).")

        output_paths: List[str] = []
        try:
            for group in groups:
                writer = PdfWriter()
                try:
                    for page_index in group:
                        writer.add_page(reader.pages[page_index])
                    out_path = new_temp_path(suffix=".pdf")
                    with open(out_path, "wb") as f:
                        writer.write(f)
                    output_paths.append(out_path)
                finally:
                    writer.close()

            logger.info(f"Split {input_path} into {len(output_paths)} file(s)")
            return output_paths
        except Exception:
            # Don't leak partially-written outputs if a later group fails.
            from utils.tempfiles import delete_paths
            delete_paths(output_paths)
            raise
