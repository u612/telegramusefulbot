"""Shared helpers for every PDF service: consistent error type and a single
place that decides how to open/validate a PDF before processing.
"""
from pypdf import PdfReader

from core.config import settings


class PDFProcessingError(RuntimeError):
    """Raised for any expected PDF processing failure (corrupt file,
    password-protected, invalid page range, etc.) -- handlers catch this
    specifically to show the user a clear message, as opposed to unexpected
    exceptions which fall through to the global error handler.
    """


def open_pdf_reader(path: str, allow_encrypted: bool = False) -> PdfReader:
    """Open and sanity-check a PDF, raising PDFProcessingError with a
    user-friendly message on any problem. Centralizing this means every
    PDF service reports encrypted/corrupt files the same way.
    """
    try:
        reader = PdfReader(path)
    except Exception as e:
        raise PDFProcessingError(f"Could not read this PDF (corrupted or unsupported format): {e}")

    if reader.is_encrypted and not allow_encrypted:
        raise PDFProcessingError(
            "This PDF is password-protected. Use 'Remove Password' first, then retry."
        )

    try:
        _ = len(reader.pages)
    except Exception as e:
        raise PDFProcessingError(f"Could not read pages from this PDF: {e}")

    return reader


def check_page_count(reader: PdfReader, min_pages: int = 1) -> int:
    count = len(reader.pages)
    if count < min_pages:
        raise PDFProcessingError(f"PDF must have at least {min_pages} page(s).")
    return count


MAX_PDF_PAGES = 2000  # sanity ceiling to avoid pathological documents hanging processing
