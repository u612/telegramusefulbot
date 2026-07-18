"""Compress a PDF's file size.

Two pieces live here:

1. `PDFCompressor.analyze()` -- a fast, PyMuPDF (fitz)-based pre-analysis
   pass: page count, embedded image count, how much text is on the page,
   and from that a Digital / Scanned / Mixed classification plus a
   recommended compression mode. Read-only, no subprocess involved.

2. `PDFCompressor.compress()` -- the actual compression, via Ghostscript
   (already a project dependency -- see Dockerfile). The previous version
   of this module only set `-dPDFSETTINGS`, which is often not enough on
   its own (many PDFs' images are already at or below a preset's default
   downsample target, so nothing happens). This version explicitly drives
   image downsampling resolution and JPEG quality per mode, which is what
   actually shrinks image-heavy PDFs. A fourth mode, Target File Size,
   searches a ladder of these profiles (best quality first) and stops at
   the first one that meets the requested size, so quality is only traded
   away as far as necessary.

Both PyMuPDF and Ghostscript are already required by this project
(requirements.txt / Dockerfile respectively) -- no new dependencies.
"""
import asyncio
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

import fitz  # PyMuPDF

from core.logger import logger
from utils.tempfiles import new_temp_path, delete_path
from services.pdf._common import PDFProcessingError, open_pdf_reader
from services.security.validator import run_subprocess_safe, SubprocessError


class CompressionMode(str, Enum):
    BEST_QUALITY = "best_quality"
    BALANCED = "balanced"
    MAXIMUM = "maximum"
    TARGET_SIZE = "target_size"


# Kept so any stray import of the old name doesn't hard-crash the app.
CompressionLevel = CompressionMode

_UNSET = object()


@dataclass(frozen=True)
class _GsProfile:
    label: str
    pdfsettings: str
    image_dpi: int      # color/gray image downsample target
    mono_dpi: int        # black & white (scanned line-art) downsample target
    jpeg_q: int          # re-encode quality for color/gray images


_PROFILES = {
    CompressionMode.BEST_QUALITY: _GsProfile("best_quality", "/printer", 200, 300, 90),
    CompressionMode.BALANCED:     _GsProfile("balanced",     "/ebook",   140, 300, 75),
    CompressionMode.MAXIMUM:      _GsProfile("maximum",      "/screen",   90, 200, 50),
}

# Ladder used to search for a Target File Size, ordered best quality first
# so the search stops as soon as a size is achieved, keeping the highest
# quality possible for that size.
_TARGET_SEARCH_LADDER: List[_GsProfile] = [
    _GsProfile("t1", "/printer", 200, 300, 88),
    _GsProfile("t2", "/ebook",   140, 300, 75),
    _GsProfile("t3", "/ebook",   110, 250, 65),
    _GsProfile("t4", "/screen",   96, 200, 55),
    _GsProfile("t5", "/screen",   72, 150, 42),
    _GsProfile("t6", "/screen",   50, 120, 30),
]

_TARGET_TOLERANCE = 1.05  # accept up to 5% over the requested target


@dataclass(frozen=True)
class PDFAnalysis:
    page_count: int
    image_count: int
    text_amount: str        # "Low" | "Medium" | "High"
    doc_type: str            # "Digital PDF" | "Scanned Document" | "Mixed Document"
    recommended: CompressionMode


class PDFCompressor:
    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    async def analyze(self, input_path: str) -> PDFAnalysis:
        return await asyncio.to_thread(self._analyze_sync, input_path)

    @staticmethod
    def _analyze_sync(input_path: str) -> PDFAnalysis:
        try:
            doc = fitz.open(input_path)
        except Exception as e:
            raise PDFProcessingError("Failed to analyze PDF.") from e

        try:
            page_count = max(doc.page_count, 1)
            image_count = 0
            total_chars = 0
            image_only_pages = 0

            for page in doc:
                images = page.get_images(full=True)
                image_count += len(images)
                chars = len(page.get_text("text").strip())
                total_chars += chars
                if images and chars < 20:
                    image_only_pages += 1
        finally:
            doc.close()

        avg_chars = total_chars / page_count

        if image_only_pages >= max(1, int(page_count * 0.6)):
            doc_type = "Scanned Document"
        elif image_count == 0:
            doc_type = "Digital PDF"
        else:
            doc_type = "Mixed Document"

        if avg_chars < 50:
            text_amount = "Low"
        elif avg_chars < 800:
            text_amount = "Medium"
        else:
            text_amount = "High"

        if doc_type == "Scanned Document":
            recommended = (
                CompressionMode.MAXIMUM if image_count > page_count * 1.2 else CompressionMode.BALANCED
            )
        elif doc_type == "Digital PDF":
            recommended = CompressionMode.BEST_QUALITY
        else:
            recommended = CompressionMode.BALANCED

        return PDFAnalysis(
            page_count=page_count,
            image_count=image_count,
            text_amount=text_amount,
            doc_type=doc_type,
            recommended=recommended,
        )

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------

    async def compress(
        self,
        input_path: str,
        mode: CompressionMode = CompressionMode.BALANCED,
        target_size_bytes: Optional[int] = None,
        timeout=_UNSET,
    ) -> Tuple[str, dict]:
        """Returns (output_path, info). `info` always has `original_size`
        and `compressed_size`; for TARGET_SIZE it also has `target_size`
        and `target_achieved` (bool). `timeout` should come from the
        caller's effective limits (utils.limits) -- pass None explicitly
        for no timeout (the owner); omit it for the settings default.
        """
        if shutil.which("gs") is None:
            raise PDFProcessingError(
                "PDF compression is currently unavailable on this server (Ghostscript not installed)."
            )
        open_pdf_reader(input_path)
        original_size = os.path.getsize(input_path)

        if mode == CompressionMode.TARGET_SIZE:
            if not target_size_bytes or target_size_bytes <= 0:
                raise PDFProcessingError("No target size specified.")
            output_path, achieved = await self._search_target_size(input_path, target_size_bytes, timeout)
            compressed_size = os.path.getsize(output_path)
            if compressed_size >= original_size:
                delete_path(output_path)
                output_path = self._fallback_copy(input_path)
                compressed_size = os.path.getsize(output_path)
                achieved = compressed_size <= target_size_bytes * _TARGET_TOLERANCE
            logger.info(
                f"Compressed {input_path} to target {target_size_bytes}B: "
                f"{original_size} -> {compressed_size} bytes (achieved={achieved})"
            )
            return output_path, {
                "original_size": original_size,
                "compressed_size": compressed_size,
                "target_size": target_size_bytes,
                "target_achieved": achieved,
            }

        profile = _PROFILES.get(mode, _PROFILES[CompressionMode.BALANCED])
        output_path = await self._run_gs(input_path, profile, timeout)
        compressed_size = os.path.getsize(output_path)

        if compressed_size >= original_size:
            delete_path(output_path)
            output_path = self._fallback_copy(input_path)
            compressed_size = os.path.getsize(output_path)

        logger.info(f"Compressed {input_path}: {original_size} -> {compressed_size} bytes ({mode.value})")
        return output_path, {"original_size": original_size, "compressed_size": compressed_size}

    @staticmethod
    def _fallback_copy(input_path: str) -> str:
        fallback_path = new_temp_path(suffix=".pdf")
        shutil.copy(input_path, fallback_path)
        logger.info("Compression did not reduce size; returning a copy of the original instead.")
        return fallback_path

    async def _run_gs(self, input_path: str, profile: _GsProfile, timeout) -> str:
        output_path = new_temp_path(suffix=".pdf")
        cmd = [
            "gs",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            f"-dPDFSETTINGS={profile.pdfsettings}",
            "-dNOPAUSE", "-dQUIET", "-dBATCH", "-dSAFER",
            "-dDetectDuplicateImages=true",
            "-dCompressFonts=true",
            "-dSubsetFonts=true",
            "-dDownsampleColorImages=true",
            f"-dColorImageResolution={profile.image_dpi}",
            "-dColorImageDownsampleType=/Bicubic",
            "-dAutoFilterColorImages=false",
            "-dColorImageFilter=/DCTEncode",
            "-dDownsampleGrayImages=true",
            f"-dGrayImageResolution={profile.image_dpi}",
            "-dGrayImageDownsampleType=/Bicubic",
            "-dAutoFilterGrayImages=false",
            "-dGrayImageFilter=/DCTEncode",
            f"-dJPEGQ={profile.jpeg_q}",
            "-dDownsampleMonoImages=true",
            f"-dMonoImageResolution={profile.mono_dpi}",
            "-dMonoImageDownsampleType=/Bicubic",
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

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            delete_path(output_path)
            raise PDFProcessingError("Compression produced no output; the file may be malformed.")
        return output_path

    async def _search_target_size(self, input_path: str, target_bytes: int, timeout) -> Tuple[str, bool]:
        """Try each profile in the ladder, best quality first, keeping
        whichever result is currently smallest, stopping as soon as one
        meets the target so quality is only traded away as far as needed.
        """
        best_path: Optional[str] = None
        best_size: Optional[int] = None

        for profile in _TARGET_SEARCH_LADDER:
            candidate_path = await self._run_gs(input_path, profile, timeout)
            candidate_size = os.path.getsize(candidate_path)

            if best_path is None or candidate_size < best_size:
                if best_path is not None:
                    delete_path(best_path)
                best_path, best_size = candidate_path, candidate_size
            else:
                delete_path(candidate_path)

            if candidate_size <= target_bytes:
                break

        return best_path, best_size <= target_bytes
