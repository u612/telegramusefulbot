"""Reduce an image's file size."""
import asyncio
from enum import Enum

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.image._common import ImageProcessingError, open_image, flatten_to_rgb


class CompressionLevel(str, Enum):
    LOW = "low"       # quality 85 -- best quality, least size reduction
    MEDIUM = "medium"  # quality 60
    HIGH = "high"      # quality 30 -- smallest size, most quality loss


_QUALITY = {CompressionLevel.LOW: 85, CompressionLevel.MEDIUM: 60, CompressionLevel.HIGH: 30}


class ImageCompressor:
    async def compress(self, input_path: str, level: CompressionLevel = CompressionLevel.MEDIUM) -> str:
        return await asyncio.to_thread(self._compress_sync, input_path, level)

    @staticmethod
    def _compress_sync(input_path: str, level: CompressionLevel) -> str:
        img = open_image(input_path)
        quality = _QUALITY.get(level, 60)
        try:
            if img.format == "JPEG":
                output_path = new_temp_path(suffix=".jpg")
                rgb = flatten_to_rgb(img)
                rgb.save(output_path, "JPEG", quality=quality, optimize=True)
            elif img.format == "PNG":
                output_path = new_temp_path(suffix=".png")
                # PNG is lossless; "compression" here means re-encoding with
                # maximum zlib effort, which usually only shaves a modest
                # amount. For a real size reduction on photographic PNGs,
                # converting to JPEG would help, but that would silently
                # drop transparency -- safer to stay lossless for PNG.
                img.save(output_path, "PNG", optimize=True, compress_level=9)
            else:
                # WEBP/BMP/TIFF/GIF: convert to JPEG, which is the only
                # reliable way to meaningfully shrink these.
                output_path = new_temp_path(suffix=".jpg")
                rgb = flatten_to_rgb(img)
                rgb.save(output_path, "JPEG", quality=quality, optimize=True)

            logger.info(f"Compressed {input_path} ({level.value}) -> {output_path}")
            return output_path
        finally:
            img.close()
