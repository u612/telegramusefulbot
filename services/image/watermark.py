"""Stamp a diagonal, semi-transparent text watermark onto an image."""
import asyncio

from PIL import Image as PILImage, ImageDraw, ImageFont

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.image._common import ImageProcessingError, open_image, flatten_to_rgb

MAX_WATERMARK_TEXT_LEN = 100


def _get_font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Older Pillow: load_default() takes no size argument.
        return ImageFont.load_default()


def _build_text_layer(text: str, font, opacity: float) -> PILImage.Image:
    dummy = PILImage.new("RGBA", (10, 10))
    d = ImageDraw.Draw(dummy)
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 10
    layer = PILImage.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(layer)
    d2.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=(160, 160, 160, int(255 * opacity)))
    return layer


class ImageWatermark:
    async def add_watermark(self, input_path: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise ImageProcessingError("Watermark text can't be empty.")
        if len(text) > MAX_WATERMARK_TEXT_LEN:
            raise ImageProcessingError(f"Watermark text is too long (max {MAX_WATERMARK_TEXT_LEN} characters).")
        return await asyncio.to_thread(self._watermark_sync, input_path, text)

    @staticmethod
    def _watermark_sync(input_path: str, text: str) -> str:
        img = open_image(input_path)
        try:
            base = img.convert("RGBA")
            font_size = max(24, min(base.width, base.height) // 12)
            font = _get_font(font_size)

            text_layer = _build_text_layer(text, font, opacity=0.35)
            rotated = text_layer.rotate(45, expand=True, resample=PILImage.BICUBIC)

            canvas = PILImage.new("RGBA", base.size, (0, 0, 0, 0))
            paste_x = (base.width - rotated.width) // 2
            paste_y = (base.height - rotated.height) // 2
            canvas.paste(rotated, (paste_x, paste_y), rotated)

            result = PILImage.alpha_composite(base, canvas)

            original_format = (img.format or "PNG").upper()
            if original_format in ("JPEG", "BMP"):
                result = flatten_to_rgb(result)
                ext = ".jpg" if original_format == "JPEG" else ".bmp"
            else:
                ext = f".{original_format.lower()}"

            output_path = new_temp_path(suffix=ext)
            result.save(output_path, original_format if original_format != "JPEG" else "JPEG")
            logger.info(f"Watermarked {input_path} -> {output_path}")
            return output_path
        finally:
            img.close()
          
