"""Tests for services/image/*. Uses real Pillow output."""
import pytest
from PIL import Image

from services.image.compressor import ImageCompressor, CompressionLevel
from services.image.resizer import ImageResizer
from services.image.cropper import ImageCropper
from services.image.rotator import ImageRotator
from services.image.flipper import ImageFlipper
from services.image.converter import ImageConverter
from services.image.watermark import ImageWatermark
from services.image.metadata import ImageMetadataStripper
from services.image._common import ImageProcessingError


async def test_compress_produces_smaller_or_valid_file(make_image):
    src = make_image("c.jpg", mode="RGB")
    out = await ImageCompressor().compress(src, CompressionLevel.HIGH)
    assert Image.open(out).format == "JPEG"


async def test_resize_exact_dimensions(make_image):
    src = make_image("r.png", size=(400, 300))
    out = await ImageResizer().resize(src, "200x100")
    assert Image.open(out).size == (200, 100)


async def test_resize_proportional_by_width(make_image):
    src = make_image("r2.png", size=(400, 300))
    out = await ImageResizer().resize(src, "200")
    assert Image.open(out).size == (200, 150)


async def test_resize_rejects_bad_spec(make_image):
    src = make_image(size=(100, 100))
    with pytest.raises(ImageProcessingError):
        await ImageResizer().resize(src, "not-a-size")


async def test_crop_produces_correct_box_size(make_image):
    src = make_image("cr.png", size=(400, 300))
    out = await ImageCropper().crop(src, "0,0,100,100")
    assert Image.open(out).size == (100, 100)


async def test_crop_rejects_invalid_box(make_image):
    src = make_image(size=(400, 300))
    with pytest.raises(ImageProcessingError):
        await ImageCropper().crop(src, "300,0,100,100")  # x1 > x2


async def test_rotate_swaps_dimensions_on_90(make_image):
    src = make_image("rot.png", size=(400, 200))
    out = await ImageRotator().rotate(src, 90)
    assert Image.open(out).size == (200, 400)


async def test_flip_horizontal_mirrors_pixels(tmp_path):
    path = str(tmp_path / "fl.png")
    img = Image.new("RGB", (10, 10), (0, 0, 0))
    img.putpixel((0, 0), (255, 255, 255))
    img.save(path)

    out = await ImageFlipper().flip(path, "horizontal")
    flipped = Image.open(out)
    assert flipped.getpixel((9, 0)) == (255, 255, 255)


async def test_convert_to_jpeg_flattens_alpha(make_image):
    src = make_image("conv.png", mode="RGBA", color=(10, 20, 30, 100))
    out = await ImageConverter().convert(src, "jpeg")
    assert Image.open(out).mode == "RGB"


async def test_watermark_preserves_size(make_image):
    src = make_image("wm.png", size=(400, 400))
    out = await ImageWatermark().add_watermark(src, "SAMPLE")
    assert Image.open(out).size == (400, 400)


async def test_watermark_rejects_empty_text(make_image):
    src = make_image(size=(100, 100))
    with pytest.raises(ImageProcessingError):
        await ImageWatermark().add_watermark(src, "")


async def test_metadata_strip_removes_exif(tmp_path):
    path = str(tmp_path / "meta.jpg")
    img = Image.new("RGB", (50, 50), (1, 2, 3))
    exif = img.getexif()
    exif[0x0131] = "TestSoftware"
    img.save(path, exif=exif)
    assert len(Image.open(path).getexif()) > 0

    out = await ImageMetadataStripper().strip(path)
    assert len(Image.open(out).getexif()) == 0
