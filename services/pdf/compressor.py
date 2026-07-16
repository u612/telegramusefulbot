"""Compress a PDF's file size using Ghostscript (lossy for embedded images,
lossless for the rest of the document structure).
"""
import shutil
from enum import Enum

from core.config import settings
from core.logger import logger
from utils.tempfiles import new_temp_path, delete_path
from services.pdf._common import PDFProcessingError, open_pdf_reader
from services.security.validator import run_subprocess_safe, SubprocessError


class CompressionLevel(str, Enum):
    LOW = "low"        # /printer -- smallest quality loss
    MEDIUM = "medium"   # /ebook -- balanced (default)
    HIGH = "high"       # /screen -- smallest file, most quality loss


_GS_SETTINGS = {
    CompressionLevel.LOW: "/printer",
    CompressionLevel.MEDIUM: "/ebook",
    CompressionLevel.HIGH: "/screen",
}


_UNSET = object()


class PDFCompressor:
    async def compress(self, input_path: str, level: CompressionLevel = CompressionLevel.MEDIUM, timeout=_UNSET) -> str:
        """Returns the path to a compressed copy of the PDF. `timeout`
        should come from the caller's effective limits (utils.limits) --
        pass None explicitly for no timeout (the owner). Omitting it falls
        back to settings.SUBPROCESS_TIMEOUT.
        """
        if shutil.which("gs") is None:
            raise PDFProcessingError(
                "PDF compression is currently unavailable on this server (Ghostscript not installed)."
            )

        # Validate the input is actually a readable PDF before shelling out.
        open_pdf_reader(input_path)

        output_path = new_temp_path(suffix=".pdf")
        gs_setting = _GS_SETTINGS.get(level, "/ebook")

        cmd = [
            "gs",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            f"-dPDFSETTINGS={gs_setting}",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            "-dSAFER",  # disallow PostScript operators that touch the filesystem
            f"-sOutputFile={output_path}",
            input_path,
        ]

        try:
            if timeout is _UNSET:
                await run_subprocess_safe(cmd)
            else:
                await run_subprocess_safe(cmd, timeout=timeout)
        except SubprocessError as e:
            delete_path(output_path)
            raise PDFProcessingError(f"Compression failed: {e}")

        import os
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            delete_path(output_path)
            raise PDFProcessingError("Compression produced no output; the file may be malformed.")

        original_size = os.path.getsize(input_path)
        new_size = os.path.getsize(output_path)
        logger.info(f"Compressed {input_path}: {original_size} -> {new_size} bytes ({level.value})")

        # Ghostscript occasionally produces a larger file for already-optimized
        # PDFs; fall back to a copy of the original rather than "compressing" it bigger.
        if new_size >= original_size:
            delete_path(output_path)
            fallback_path = new_temp_path(suffix=".pdf")
            shutil.copy(input_path, fallback_path)
            logger.info("Compressed output was not smaller than the original; returning original instead.")
            return fallback_path

        return output_path
