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

   Best Quality / Balanced / Maximum are three genuinely different
   Ghostscript parameter sets (resolution, JPEG quality, and -- the part
   the previous version was missing -- the image *downsample threshold*,
   which controls whether an image close to the target resolution gets
   touched at all). Target File Size no longer walks a fixed ladder and
   stops at the first profile under the target; it binary-searches JPEG
   quality within each resolution tier so the result is the closest
   possible size at or under the target, never over it.

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
# least this fraction of the original size. Below this, Problem 3's
# escalation kicks in (for modes that support it) before we conclude the
# PDF is genuinely already optimized.
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
# every-day tradeoff. Maximum uses the lowest resolution/quality *and*
# threshold=1.0, so -- unlike the old /screen-only setting -- it actually
# forces every image down to its target DPI even if a prior compression
# pass already left it "close enough", which is what was causing already
# -compressed files to fall straight through to "already optimized".
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

# Escalation fallback used only when the assigned profile's result doesn't
# clear _MEANINGFUL_REDUCTION -- see Problem 3. Each mode escalates to a
# strictly more aggressive profile before we're willing to call a file
# "already optimized". Best Quality intentionally has no escalation: a
# small reduction is its whole point, not a bug.
_ESCALATION = {
    CompressionMode.BALANCED: _GsProfile(
        "balanced_escalated", "/screen", image_dpi=100, mono_dpi=200, jpeg_q=55,
        downsample_threshold=1.0, clean="gc",
    ),
    CompressionMode.MAXIMUM: _GsProfile(
        "maximum_escalated", "/screen", image_dpi=55, mono_dpi=110, jpeg_q=25,
        downsample_threshold=1.0, clean="strip",
    ),
}

# Resolution tiers for the Target File Size search, high to low. For each
# tier we binary-search JPEG quality to find the largest (closest-to
# -target) size that still doesn't exceed the target, then stop at the
# first (highest-resolution) tier where that's achievable at all --
# a lower tier can only produce a same-or-smaller ceiling, so it can't
# beat what a higher tier already found.
_TARGET_DPI_TIERS: List[int] = [300, 240, 190, 150, 120, 96, 75, 60, 45]
_TARGET_Q_MIN = 20
_TARGET_Q_MAX = 95


def _tier_profile(dpi: int, jpeg_q: int) -> _GsProfile:
    pdfsettings = "/printer" if dpi >= 200 else ("/ebook" if dpi >= 110 else "/screen")
    mono_dpi = max(120, int(dpi * 1.6))
    return _GsProfile(
        f"tier_{dpi}_{jpeg_q}", pdfsettings, image_dpi=dpi, mono_dpi=mono_dpi,
        jpeg_q=jpeg_q, downsample_threshold=1.0, clean="gc",
    )


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

        # Problem 3: don't declare "already optimized" after a single
        # attempt. If this mode has an escalation profile and the result
        # didn't clear the meaningful-reduction bar, genuinely try harder
        # before giving up.
        escalation = _ESCALATION.get(mode)
        if escalation is not None and original_size > 0:
            reduction = (original_size - compressed_size) / original_size
            if reduction < _MEANINGFUL_REDUCTION:
                escalated_path = await self._run_gs_clean(input_path, escalation, timeout)
                escalated_size = os.path.getsize(escalated_path)
                if escalated_size < compressed_size:
                    delete_path(output_path)
                    output_path, compressed_size = escalated_path, escalated_size
                else:
                    delete_path(escalated_path)

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
        """Run Ghostscript, then (for modes that ask for it) a PyMuPDF
        object-optimization / metadata-cleanup pass on top: garbage
        -collect unused objects and re-deflate streams ("gc"), or do
        that plus strip document metadata entirely ("strip", Maximum
        only). Best Quality skips this ("none") to avoid touching
        anything beyond image re-encoding.
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
        """Binary-searches JPEG quality within each resolution tier
        (highest first) to find the largest file that still does not
        exceed `target_bytes`. Never returns a candidate above the
        target if any candidate at or under it was found anywhere in
        the search; only falls back to the smallest achievable size
        (which may exceed the target) if the target is below what the
        PDF can physically be compressed to.
        """
        best_path: Optional[str] = None
        best_size: Optional[int] = None
        smallest_seen_path: Optional[str] = None
        smallest_seen_size: Optional[int] = None

        def _keep_smallest_seen(path: str, size: int):
            nonlocal smallest_seen_path, smallest_seen_size
            if smallest_seen_size is None or size < smallest_seen_size:
                if smallest_seen_path is not None:
                    delete_path(smallest_seen_path)
                smallest_seen_path, smallest_seen_size = path, size
            else:
                delete_path(path)

        for dpi in _TARGET_DPI_TIERS:
            # Try the top of the quality range for this tier first.
            high_path = await self._run_gs(input_path, _tier_profile(dpi, _TARGET_Q_MAX), timeout)
            high_size = os.path.getsize(high_path)

            if high_size <= target_bytes:
                # Whole tier fits even at max quality -- this is the
                # closest result available at this (highest-so-far)
                # resolution; no need to search further tiers.
                best_path, best_size = high_path, high_size
                break

            _keep_smallest_seen(high_path, high_size)

            low_path = await self._run_gs(input_path, _tier_profile(dpi, _TARGET_Q_MIN), timeout)
            low_size = os.path.getsize(low_path)

            if low_size > target_bytes:
                # Not achievable at all at this resolution; drop to the
                # next lower tier.
                _keep_smallest_seen(low_path, low_size)
                continue

            # Straddles the target within this tier: binary-search
            # quality to find the largest value that still fits.
            lo, hi = _TARGET_Q_MIN, _TARGET_Q_MAX
            candidate_path, candidate_size = low_path, low_size
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                mid_path = await self._run_gs(input_path, _tier_profile(dpi, mid), timeout)
                mid_size = os.path.getsize(mid_path)
                if mid_size <= target_bytes:
                    delete_path(candidate_path)
                    candidate_path, candidate_size = mid_path, mid_size
                    lo = mid
                else:
                    _keep_smallest_seen(mid_path, mid_size)
                    hi = mid
            delete_path(high_path)
            best_path, best_size = candidate_path, candidate_size
            break

        if best_path is not None:
            if smallest_seen_path is not None:
                delete_path(smallest_seen_path)
            return best_path, True

        # Target was below what's physically achievable -- return the
        # smallest size found across the whole search as the best-effort
        # result and flag that the target wasn't reached.
        return smallest_seen_path, False
