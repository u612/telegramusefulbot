"""Stamp a text or image watermark onto every page of a PDF.

Implemented entirely with PyMuPDF (fitz) per project requirements:
  - better text rendering & positioning than a pypdf/reportlab overlay
  - native opacity support (fill_opacity, and for images via a pre-baked
    alpha channel)
  - native rotation support (via Shape.insert_text morph matrices)
  - operates directly on the page content stream, so scanned/image-only
    PDFs are handled the same as any other PDF
"""
import asyncio
from typing import Optional, Tuple

import fitz  # PyMuPDF
from PIL import Image, UnidentifiedImageError

from core.logger import logger
from utils.tempfiles import new_temp_path, delete_path
from services.pdf._common import PDFProcessingError

MAX_WATERMARK_TEXT_LEN = 100

# --------------------------------------------------------------------------
# Shared position/opacity helpers
# --------------------------------------------------------------------------

_MARGIN_RATIO = 0.06  # margin from page edge, as a fraction of the shorter side

POSITIONS = {
    "center", "top_left", "top_right", "bottom_left", "bottom_right",
    "header", "footer",
}
TEXT_ROTATIONS = {"diagonal": 45, "reverse_diagonal": -45, "horizontal": 0, "vertical": 90}
OPACITIES = {10: 0.10, 25: 0.25, 50: 0.50, 75: 0.75, 100: 1.0}
TEXT_SIZES = {"small", "medium", "large", "auto"}
IMAGE_SIZES = {"small", "medium", "large", "original"}

# Predefined text-watermark colors (RGB, 0-1 floats per channel -- PyMuPDF
# convention). Kept muted/legible rather than pure primaries so the
# watermark stays readable at any opacity.
TEXT_COLORS = {
    "black": (0.0, 0.0, 0.0),
    "white": (1.0, 1.0, 1.0),
    "red": (0.80, 0.10, 0.10),
    "blue": (0.10, 0.30, 0.80),
    "green": (0.10, 0.55, 0.20),
    "yellow": (0.85, 0.75, 0.10),
    "purple": (0.50, 0.15, 0.60),
    "orange": (0.90, 0.50, 0.10),
    "brown": (0.50, 0.30, 0.10),
    "pink": (0.95, 0.55, 0.65),
}

# Predefined text-watermark styles, mapped to PyMuPDF's built-in base-14
# Helvetica font names.
TEXT_STYLES = {
    "normal": "helv",
    "bold": "hebo",
    "italic": "heit",
    "bold_italic": "hebi",
}


def _opacity_fraction(opacity_pct: int) -> float:
    """Validate and convert a 1-100 opacity percentage to a 0-1 fraction.
    Replaces the old fixed OPACITIES lookup so any value in range
    (including the new custom-opacity input) is accepted, not just the
    five preset percentages.
    """
    if not isinstance(opacity_pct, int) or isinstance(opacity_pct, bool) or not (1 <= opacity_pct <= 100):
        raise PDFProcessingError("Invalid watermark opacity.")
    return opacity_pct / 100.0


def _margin(width: float, height: float) -> float:
    return min(width, height) * _MARGIN_RATIO


def _anchor_point(position: str, width: float, height: float) -> Tuple[float, float, str]:
    """Return (x, y, halign) where halign in {'left','center','right'}
    describing how text/image should be aligned relative to (x, y).
    """
    m = _margin(width, height)
    if position == "center":
        return width / 2, height / 2, "center"
    if position == "top_left":
        return m, m, "left"
    if position == "top_right":
        return width - m, m, "right"
    if position == "bottom_left":
        return m, height - m, "left"
    if position == "bottom_right":
        return width - m, height - m, "right"
    if position == "header":
        return width / 2, m, "center"
    if position == "footer":
        return width / 2, height - m, "center"
    raise PDFProcessingError("Invalid watermark position.")


# --------------------------------------------------------------------------
# Text watermark
# --------------------------------------------------------------------------

def _text_fontsize(width: float, height: float, size: str, text: str) -> float:
    base = min(width, height)
    if size == "small":
        return max(base * 0.035, 10)
    if size == "medium":
        return max(base * 0.06, 12)
    if size == "large":
        return max(base * 0.10, 14)
    # auto: scale so the text roughly fits within ~70% of the shorter side
    target_width = width * 0.7
    fontsize = max(base * 0.06, 12)
    try:
        text_width = fitz.get_text_length(text, fontname="helv", fontsize=fontsize)
        if text_width > 0:
            fontsize = max(10, min(fontsize * (target_width / text_width), base * 0.18))
    except Exception:
        pass
    return fontsize


def _draw_text_watermark(page: "fitz.Page", text: str, position: str, rotation_deg: int,
                          opacity: float, fontsize: float, color: Tuple[float, float, float],
                          fontname: str) -> None:
    rect = page.rect
    width, height = rect.width, rect.height
    x, y, halign = _anchor_point(position, width, height)

    text_width = fitz.get_text_length(text, fontname=fontname, fontsize=fontsize)
    if halign == "center":
        origin_x = x - text_width / 2
    elif halign == "right":
        origin_x = x - text_width
    else:
        origin_x = x
    origin_y = y + fontsize / 3  # roughly vertically center on the anchor

    point = fitz.Point(origin_x, origin_y)
    pivot = fitz.Point(x, y)
    matrix = fitz.Matrix(1, 1).prerotate(-rotation_deg)

    shape = page.new_shape()
    shape.insert_text(
        point,
        text,
        fontname=fontname,
        fontsize=fontsize,
        color=color,
        fill_opacity=opacity,
        morph=(pivot, matrix),
    )
    shape.commit(overlay=True)


def _watermark_text_sync(input_path: str, text: str, position: str, rotation: str,
                          opacity_pct: int, size: str, color: str = "black",
                          style: str = "normal") -> str:
    if position not in POSITIONS:
        raise PDFProcessingError("Invalid watermark position.")
    if rotation not in TEXT_ROTATIONS:
        raise PDFProcessingError("Invalid watermark rotation.")
    if size not in TEXT_SIZES:
        raise PDFProcessingError("Invalid watermark size.")
    if color not in TEXT_COLORS:
        raise PDFProcessingError("Invalid watermark color.")
    if style not in TEXT_STYLES:
        raise PDFProcessingError("Invalid watermark style.")

    opacity = _opacity_fraction(opacity_pct)

    doc = fitz.open(input_path)
    try:
        if doc.page_count == 0:
            raise PDFProcessingError("PDF must have at least 1 page(s).")
        rotation_deg = TEXT_ROTATIONS[rotation]
        rgb = TEXT_COLORS[color]
        fontname = TEXT_STYLES[style]
        for page in doc:
            fontsize = _text_fontsize(page.rect.width, page.rect.height, size, text)
            _draw_text_watermark(page, text, position, rotation_deg, opacity, fontsize, rgb, fontname)

        output_path = new_temp_path(suffix=".pdf")
        doc.save(output_path)
        logger.info(f"Text-watermarked {input_path} -> {output_path}")
        return output_path
    except PDFProcessingError:
        raise
    except Exception as e:
        logger.exception(f"Text watermark failed for {input_path}: {e}")
        raise PDFProcessingError("Failed to apply watermark.") from e
    finally:
        doc.close()


# --------------------------------------------------------------------------
# Image watermark
# --------------------------------------------------------------------------

def _image_scale_factor(size: str) -> Optional[float]:
    """Fraction of the page's shorter side the image's longer side should
    occupy. None means 'original' -- use the image's native pixel size
    (converted at 96 DPI) unscaled (but still capped to the page).
    """
    return {"small": 0.18, "medium": 0.30, "large": 0.45}.get(size)


def _prepare_watermark_image(image_path: str, opacity: float) -> str:
    """Bake the requested opacity into the image's alpha channel and save
    as a temp PNG -- PyMuPDF's insert_image() respects PNG transparency,
    which is the most reliable way to get per-image opacity across
    PyMuPDF versions.
    """
    try:
        img = Image.open(image_path)
        img = img.convert("RGBA")
    except (UnidentifiedImageError, OSError) as e:
        raise PDFProcessingError("Invalid or unsupported watermark image.") from e

    r, g, b, a = img.split()
    a = a.point(lambda v: int(v * opacity))
    img.putalpha(a)

    out_path = new_temp_path(suffix=".png")
    img.save(out_path, format="PNG")
    return out_path


def _draw_image_watermark(page: "fitz.Page", image_path: str, position: str, size: str) -> None:
    rect = page.rect
    width, height = rect.width, rect.height

    with Image.open(image_path) as im:
        img_w_px, img_h_px = im.size
    aspect = img_w_px / img_h_px if img_h_px else 1.0

    scale = _image_scale_factor(size)
    if scale is not None:
        target_long = min(width, height) * scale
        if aspect >= 1:
            draw_w = target_long
            draw_h = target_long / aspect
        else:
            draw_h = target_long
            draw_w = target_long * aspect
    else:
        # "Original": use native pixel size at 96 DPI, capped to the page
        # so it can never render larger than the page itself.
        draw_w = img_w_px * 72.0 / 96.0
        draw_h = img_h_px * 72.0 / 96.0
        max_w, max_h = width * 0.9, height * 0.9
        if draw_w > max_w or draw_h > max_h:
            factor = min(max_w / draw_w, max_h / draw_h)
            draw_w *= factor
            draw_h *= factor

    x, y, halign = _anchor_point(position, width, height)
    if halign == "center":
        x0 = x - draw_w / 2
    elif halign == "right":
        x0 = x - draw_w
    else:
        x0 = x
    y0 = y - draw_h / 2

    target_rect = fitz.Rect(x0, y0, x0 + draw_w, y0 + draw_h)
    page.insert_image(target_rect, filename=image_path, overlay=True, keep_proportion=True)


def _watermark_image_sync(input_path: str, image_path: str, position: str, size: str,
                           opacity_pct: int) -> str:
    if position not in POSITIONS:
        raise PDFProcessingError("Invalid watermark position.")
    if size not in IMAGE_SIZES:
        raise PDFProcessingError("Invalid watermark size.")

    prepared_image = _prepare_watermark_image(image_path, _opacity_fraction(opacity_pct))
    doc = None
    try:
        doc = fitz.open(input_path)
        if doc.page_count == 0:
            raise PDFProcessingError("PDF must have at least 1 page(s).")
        for page in doc:
            _draw_image_watermark(page, prepared_image, position, size)

        output_path = new_temp_path(suffix=".pdf")
        doc.save(output_path)
        logger.info(f"Image-watermarked {input_path} -> {output_path}")
        return output_path
    except PDFProcessingError:
        raise
    except Exception as e:
        logger.exception(f"Image watermark failed for {input_path}: {e}")
        raise PDFProcessingError("Failed to apply watermark.") from e
    finally:
        if doc is not None:
            doc.close()
        delete_path(prepared_image)


# --------------------------------------------------------------------------
# Public async API
# --------------------------------------------------------------------------

class PDFWatermark:
    async def add_text_watermark(self, input_path: str, text: str, position: str,
                                  rotation: str, opacity_pct: int, size: str,
                                  color: str = "black", style: str = "normal") -> str:
        text = (text or "").strip()
        if not text:
            raise PDFProcessingError("Watermark text can't be empty.")
        if len(text) > MAX_WATERMARK_TEXT_LEN:
            raise PDFProcessingError(f"Watermark text is too long (max {MAX_WATERMARK_TEXT_LEN} characters).")
        return await asyncio.to_thread(
            _watermark_text_sync, input_path, text, position, rotation, opacity_pct, size, color, style
        )

    async def add_image_watermark(self, input_path: str, image_path: str, position: str,
                                   size: str, opacity_pct: int) -> str:
        return await asyncio.to_thread(
            _watermark_image_sync, input_path, image_path, position, size, opacity_pct
        )

    # Kept for backward compatibility with any external caller still using
    # the old simple signature (plain diagonal text watermark, defaults).
    async def add_watermark(self, input_path: str, text: str) -> str:
        return await self.add_text_watermark(
            input_path, text, position="center", rotation="diagonal",
            opacity_pct=25, size="auto",
        )
