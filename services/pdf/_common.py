"""Shared helpers for every PDF service: consistent error type and a single
place that decides how to open/validate a PDF before processing.
"""
from pypdf import PdfReader
from pypdf.errors import EmptyFileError, PdfReadError

from core.config import settings
from core.logger import logger


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

    Note on "false positive" password detection: pypdf sets
    ``reader.is_encrypted`` for any PDF with an /Encrypt dictionary, even
    ones that only use an *empty* user password to restrict permissions
    (printing/copying) -- a very common output of scanners, "print to
    PDF" tools, and some office suites. Those files open fine in any PDF
    viewer without ever prompting for a password, so we try an empty-
    password decrypt before concluding the file is genuinely protected.
    """
    try:
        reader = PdfReader(path)
    except EmptyFileError as e:
        raise PDFProcessingError("Invalid or corrupted PDF.") from e
    except PdfReadError as e:
        # pypdf raises this both for structurally broken PDFs and for
        # files that aren't PDFs at all (bad header). Distinguish using
        # the header check pypdf itself performs.
        try:
            with open(path, "rb") as f:
                header = f.read(5)
        except OSError:
            header = b""
        if not header.startswith(b"%PDF-"):
            raise PDFProcessingError("Unsupported PDF format.") from e
        raise PDFProcessingError("Invalid or corrupted PDF.") from e
    except PDFProcessingError:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error opening PDF {path}: {e}")
        raise PDFProcessingError("Failed to process PDF.") from e

    if reader.is_encrypted:
        # Empty-password ("owner-only") encryption: decrypt("") succeeds
        # and the file is fully readable -- not really "protected" from
        # the user's point of view, so let it through either way.
        try:
            decrypt_result = reader.decrypt("")
        except Exception:
            decrypt_result = 0

        if not decrypt_result and not allow_encrypted:
            raise PDFProcessingError(
                "Password protected PDF. Use 'Remove Password' first, then retry."
            )

    try:
        _ = len(reader.pages)
    except PdfReadError as e:
        raise PDFProcessingError("Invalid or corrupted PDF.") from e
    except Exception as e:
        logger.exception(f"Unexpected error reading pages from PDF {path}: {e}")
        raise PDFProcessingError("Failed to process PDF.") from e

    return reader


def check_page_count(reader: PdfReader, min_pages: int = 1) -> int:
    count = len(reader.pages)
    if count < min_pages:
        raise PDFProcessingError(f"PDF must have at least {min_pages} page(s).")
    return count


MAX_PDF_PAGES = 2000  # sanity ceiling to avoid pathological documents hanging processing
