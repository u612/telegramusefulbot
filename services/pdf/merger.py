"""Merge multiple PDFs into one."""
import asyncio
from typing import List

from pypdf import PdfWriter
from pypdf.errors import DependencyError

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.pdf._common import (
    PDFProcessingError,
    open_pdf_reader,
    MAX_PDF_PAGES,
)


class PDFMerger:
    async def merge(self, input_paths: List[str]) -> str:
        """
        Merge PDFs in the given order.

        Runs inside a worker thread because pypdf is CPU/blocking.
        """
        if len(input_paths) < 2:
            raise PDFProcessingError("Need at least 2 PDF files to merge.")

        return await asyncio.to_thread(self._merge_sync, input_paths)

    @staticmethod
    def _merge_sync(input_paths: List[str]) -> str:
        writer = PdfWriter()
        total_pages = 0

        try:
            for path in input_paths:
                try:
                    reader = open_pdf_reader(path)

                    total_pages += len(reader.pages)

                    if total_pages > MAX_PDF_PAGES:
                        raise PDFProcessingError(
                            f"Combined document exceeds the {MAX_PDF_PAGES}-page limit."
                        )

                    writer.append(reader)

                except DependencyError as e:
                    logger.exception("Missing cryptography dependency while merging PDF.")
                    raise PDFProcessingError(
                        "This PDF uses AES encryption. Install the 'cryptography' package on the server."
                    ) from e

                except PDFProcessingError:
                    raise

                except Exception as e:
                    logger.exception(f"Failed while reading PDF: {path}")
                    raise PDFProcessingError(
                        "One of the uploaded PDFs is invalid, encrypted, or unsupported."
                    ) from e

            output_path = new_temp_path(suffix=".pdf")

            with open(output_path, "wb") as f:
                writer.write(f)

            logger.info(
                f"Merged {len(input_paths)} PDFs ({total_pages} pages) -> {output_path}"
            )

            return output_path

        finally:
            try:
                writer.close()
            except Exception:
                pass
