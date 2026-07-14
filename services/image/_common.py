"""Shared helpers for image services."""
from PIL import Image, UnidentifiedImageError


class ImageProcessingError(RuntimeError):
    """Raised for any expected image processing failure."""


MAX_MEGAPIXELS = 40_000_000  # sanity ceiling to avoid pathological memory usage


def open_image(path: str) -> Image.Image:
    try:
        img = Image.open(path)
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ImageProcessingError(f"Could not read this image (corrupted or unsupported format): {e}")

    if img.width * img.height > MAX_MEGAPIXELS:
        raise ImageProcessingError(
            f"Image is too large ({img.width}x{img.height}) to process safely."
        )
    return img


def flatten_to_rgb(img: Image.Image, background=(255, 255, 255)) -> Image.Image:
    """Flatten any transparency onto a solid background and return an RGB
    image -- needed before saving to formats without alpha support (JPEG, BMP).
    """
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", img.size, background)
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img
