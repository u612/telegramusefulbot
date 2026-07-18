"""Compress a PDF's file size.

Two pieces live here:

1. `PDFCompressor.analyze()` -- a fast, PyMuPDF (fitz)-based pre-analysis
   pass: page count, embedded image count, how much text is on the page,
   and from that a Digital / Scanned / Mixed classification plus a
   recommended compression mode. Read-only, no subprocess involved.

2. `PDFCompressor.compress()` -- the actual compression, via Ghostscript
   (already a project dependency -- see Dockerfile), with a PyMuPDF
   cleanup pass (garbage-collect unused objects, re-deflate streams, and
   for Maximum also strip metadata) on top -- also already a dependency,
   so still no new packages.

   Every mode now has a small, fixed upper bound on how many times
   Ghostscript can run, so nothing can hang a Telegram bot:
     - Best Quality / Balanced: exactly 1 Ghostscript run.
     - Maximum: 1 run, plus at most 1 stronger fallback if the first
       run didn't achieve a meaningful reduction (2 total, never more).
     - Target File Size: runs a fixed list of at most 6 profiles
       (high quality -> low), stopping as soon as one is small enough,
       and picks the largest one that's still <= the target. No binary
       search, no per-PDF convergence loop -- worst case is 6 Ghostscript
       calls, same as best case for a target that's hard to hit.

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

# A result is only considered a "meaningful" reduction if it saves at
# least this fraction of the original size. Below this, Maximum's single
# fallback attempt (see _MAXIMUM_FALLBACK) kicks in before we're willing
# to call the PDF genuinely already optimized.
_MEANINGFUL_REDUCTION = 0.03


@dataclass(frozen=True)
class _GsProfile:
    label: str
    pdfsettings: str
    image_dpi: int          # color/gray image downsample target
    mono_dpi: int            # black & white (scanned line-art) downsample target
    jpeg_q: int              # re-encode quality for color/gray images
    downsample_threshold: float  # gs only downsamples if image is this many
                                  # times larger than the target res; 1.5 is
                                  # gs's own default (skips images already
                                  # "close enough"), 1.0 forces every image
                                  # at or above the target down to it.
    clean: str = "none"     # "none" | "gc" | "strip" -- PyMuPDF post-pass


# Three deliberately different strategies -- not small variations of one
# another. Best Quality barely touches images and never forces a
# downsample unless an image is grossly oversized. Balanced is the
# every-day tradeoff, one Ghostscript run, no retries. Maximum uses the
# lowest resolution/quality *and* threshold=1.0, so it actually forces
# every image down to its target DPI even if a prior compression pass
# already left it "close enough".
_PROFILES = {
    CompressionMode.BEST_QUALITY: _GsProfile(
        "best_quality", "/prepress", image_dpi=250, mono_dpi=450, jpeg_q=95,
        downsample_threshold=2.0, clean="none",
    ),
    CompressionMode.BALANCED: _GsProfile(
        "balanced", "/ebook", image_dpi=150, mono_dpi=300, jpeg_q=75,
        downsample_threshold=1.5, clean="gc",
    ),
    CompressionMode.MAXIMUM: _GsProfile(
        "maximum", "/screen", image_dpi=72, mono_dpi=150, jpeg_q=38,
        downsample_threshold=1.0, clean="strip",
    ),
}

# Maximum's single allowed fallback (see Problem 2 / Problem 4): if the
# primary Maximum profile doesn't clear _MEANINGFUL_REDUCTION, try this
# ONE strictly-more-aggressive profile, then stop no matter what. Total
# Ghostscript executions for Maximum is therefore capped at 2, always.
_MAXIMUM_FALLBACK = _GsProfile(
    "maximum_fallback", "/screen", image_dpi=55, mono_dpi=110, jpeg_q=25,
    downsample_threshold=1.0, clean="strip",
)

# Fixed profile ladder for Target File Size -- high quality/size to low.
# At most one Ghostscript run per entry, at most len() entries total, so
# the whole search is bounded at a hard 6 runs. No binary search, no
# convergence loop: pick whichever prebuilt profile is closest.
_TARGET_LADDER: List[_GsProfile] = [
    _GsProfile("target_1", "/printer", image_dpi=220, mono_dpi=350, jpeg_q=90,
               downsample_threshold=1.0, clean="gc"),
    _GsProfile("target_2", "/printer", image_dpi=170, mono_dpi=280, jpeg_q=78,
               downsample_threshold=1.0, clean="gc"),
    _GsProfile("target_3", "/ebook", image_dpi=130, mono_dpi=220, jpeg_q=65,
               downsample_threshold=1.0, clean="gc"),
    _GsProfile("target_4", "/ebook", image_dpi=100, mono_dpi=170, jpeg_q=52,
               downsample_threshold=1.0, clean="gc"),
    _GsProfile("target_5", "/screen", image_dpi=75, mono_dpi=130, jpeg_q=38,
               downsample_threshold=1.0, clean="strip"),
    _GsProfile("target_6", "/screen", image_dpi=50, mono_dpi=100, jpeg_q=25,
               downsample_threshold=1.0, clean="strip"),
]


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

        Ghostscript execution counts are hard-capped per mode: Best
        Quality/Balanced = 1, Maximum = 2, Target File Size = 6. None of
        these can loop or grow with PDF size/content.
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
                achieved = compressed_size <= target_size_bytes
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
        output_path = await self._run_gs_clean(input_path, profile, timeout)
        compressed_size = os.path.getsize(output_path)

        # Maximum only: one bounded fallback attempt (never a loop) if
        # the first run didn't achieve a meaningful reduction. Best
        # Quality and Balanced never retry -- exactly 1 Ghostscript run.
        if mode == CompressionMode.MAXIMUM and original_size > 0:
            reduction = (original_size - compressed_size) / original_size
            if reduction < _MEANINGFUL_REDUCTION:
                fallback_path = await self._run_gs_clean(input_path, _MAXIMUM_FALLBACK, timeout)
                fallback_size = os.path.getsize(fallback_path)
                if fallback_size < compressed_size:
                    delete_path(output_path)
                    output_path, compressed_size = fallback_path, fallback_size
                else:
                    delete_path(fallback_path)
                # Whether or not the fallback helped, we stop here --
                # this is the one and only retry Maximum is allowed.

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
            "-dAutoRotatePages=/None",
            "-dDetectDuplicateImages=true",
            "-dCompressFonts=true",
            "-dSubsetFonts=true",
            "-dDownsampleColorImages=true",
            f"-dColorImageResolution={profile.image_dpi}",
            f"-dColorImageDownsampleThreshold={profile.downsample_threshold}",
            "-dColorImageDownsampleType=/Bicubic",
            "-dAutoFilterColorImages=false",
            "-dColorImageFilter=/DCTEncode",
            "-dDownsampleGrayImages=true",
            f"-dGrayImageResolution={profile.image_dpi}",
            f"-dGrayImageDownsampleThreshold={profile.downsample_threshold}",
            "-dGrayImageDownsampleType=/Bicubic",
            "-dAutoFilterGrayImages=false",
            "-dGrayImageFilter=/DCTEncode",
            f"-dJPEGQ={profile.jpeg_q}",
            "-dDownsampleMonoImages=true",
            f"-dMonoImageResolution={profile.mono_dpi}",
            f"-dMonoImageDownsampleThreshold={profile.downsample_threshold}",
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

    async def _run_gs_clean(self, input_path: str, profile: _GsProfile, timeout) -> str:
        """Run Ghostscript, then (for modes/tiers that ask for it) a
        PyMuPDF object-optimization / metadata-cleanup pass on top: gc
        -collect unused objects and re-deflate streams ("gc"), or do
        that plus strip document metadata entirely ("strip"). Best
        Quality skips this ("none") to avoid touching anything beyond
        image re-encoding. This is a single extra local operation, not a
        Ghostscript run, so it doesn't count against any execution cap.
        """
        gs_output = await self._run_gs(input_path, profile, timeout)
        if profile.clean == "none":
            return gs_output

        cleaned_path = new_temp_path(suffix=".pdf")
        try:
            def _clean_sync():
                doc = fitz.open(gs_output)
                try:
                    if profile.clean == "strip":
                        doc.set_metadata({})
                    doc.save(cleaned_path, garbage=4, deflate=True, clean=True)
                finally:
                    doc.close()

            await asyncio.to_thread(_clean_sync)
        except Exception as e:
            logger.warning(f"PyMuPDF cleanup pass failed, using Ghostscript output as-is: {e}")
            delete_path(cleaned_path)
            return gs_output

        if not os.path.exists(cleaned_path) or os.path.getsize(cleaned_path) == 0:
            delete_path(cleaned_path)
            return gs_output

        # Keep whichever is actually smaller -- the cleanup pass is meant
        # to help, never to regress size.
        if os.path.getsize(cleaned_path) < os.path.getsize(gs_output):
            delete_path(gs_output)
            return cleaned_path
        delete_path(cleaned_path)
        return gs_output

    async def _search_target_size(self, input_path: str, target_bytes: int, timeout) -> Tuple[str, bool]:
        """Runs the fixed _TARGET_LADDER (high quality -> low), at most
        one Ghostscript execution per entry. Stops as soon as an entry's
        output is <= target_bytes -- since the ladder is ordered from
        largest-expected-output to smallest, the first one that fits is
        already the largest (closest) valid result, so there's no need
        to keep going. If nothing in the ladder fits, returns the
        smallest candidate seen and flags target_achieved=False. Hard
        upper bound: len(_TARGET_LADDER) == 6 Ghostscript runs, always.
        """
        smallest_path: Optional[str] = None
        smallest_size: Optional[int] = None

        for profile in _TARGET_LADDER:
            candidate_path = await self._run_gs(input_path, profile, timeout)
            candidate_size = os.path.getsize(candidate_path)

            if candidate_size <= target_bytes:
                if smallest_path is not None:
                    delete_path(smallest_path)
                return candidate_path, True

            if smallest_size is None or candidate_size < smallest_size:
                if smallest_path is not None:
                    delete_path(smallest_path)
                smallest_path, smallest_size = candidate_path, candidate_size
            else:
                delete_path(candidate_path)

        # Exhausted the ladder without hitting the target -- best effort.
        return smallest_path, False
