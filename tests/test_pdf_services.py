"""Tests for services/pdf/*. Uses real pypdf/reportlab output, not mocks --
these exercise the actual byte-level PDF operations.
"""
import pytest
from pypdf import PdfReader

from services.pdf.merger import PDFMerger
from services.pdf.splitter import PDFSplitter
from services.pdf.rotator import PDFRotator
from services.pdf.extractor import PDFExtractor
from services.pdf.rearranger import PDFRearranger
from services.pdf.watermark import PDFWatermark
from services.pdf.password import PDFPassword
from services.pdf.image_to_pdf import ImageToPDF
from services.pdf._common import PDFProcessingError


async def test_merge_combines_page_counts(make_pdf):
    a = make_pdf("a.pdf", pages=2, text="A")
    b = make_pdf("b.pdf", pages=3, text="B")
    out = await PDFMerger().merge([a, b])
    assert len(PdfReader(out).pages) == 5


async def test_merge_rejects_single_file(make_pdf):
    a = make_pdf(pages=1)
    with pytest.raises(PDFProcessingError):
        await PDFMerger().merge([a])


async def test_split_into_individual_pages(make_pdf):
    src = make_pdf(pages=6)
    outputs = await PDFSplitter().split(src, None)
    assert len(outputs) == 6
    for path in outputs:
        assert len(PdfReader(path).pages) == 1


async def test_split_by_range_groups(make_pdf):
    src = make_pdf(pages=6)
    outputs = await PDFSplitter().split(src, "1-2;3-4;5-6")
    assert len(outputs) == 3
    for path in outputs:
        assert len(PdfReader(path).pages) == 2


async def test_split_rejects_out_of_range(make_pdf):
    src = make_pdf(pages=3)
    with pytest.raises(PDFProcessingError):
        await PDFSplitter().split(src, "1-99")


async def test_rotate_sets_rotation(make_pdf):
    src = make_pdf(pages=1)
    out = await PDFRotator().rotate(src, 90)
    assert PdfReader(out).pages[0].rotation == 90


async def test_rotate_rejects_invalid_angle(make_pdf):
    src = make_pdf(pages=1)
    with pytest.raises(PDFProcessingError):
        await PDFRotator().rotate(src, 45)


async def test_extract_selects_correct_pages(make_pdf):
    src = make_pdf(pages=10)
    out = await PDFExtractor().extract(src, "1-3,5,9")
    assert len(PdfReader(out).pages) == 5


async def test_extract_rejects_out_of_range_page(make_pdf):
    src = make_pdf(pages=10)
    with pytest.raises(PDFProcessingError):
        await PDFExtractor().extract(src, "1,99")


async def test_rearrange_preserves_page_count(make_pdf):
    src = make_pdf(pages=3)
    out = await PDFRearranger().rearrange(src, "3,1,2")
    assert len(PdfReader(out).pages) == 3


async def test_rearrange_rejects_incomplete_permutation(make_pdf):
    src = make_pdf(pages=3)
    with pytest.raises(PDFProcessingError):
        await PDFRearranger().rearrange(src, "1,2")


async def test_rearrange_rejects_duplicate_page(make_pdf):
    src = make_pdf(pages=3)
    with pytest.raises(PDFProcessingError):
        await PDFRearranger().rearrange(src, "1,1,2")


async def test_watermark_preserves_pages_and_adds_text(make_pdf):
    src = make_pdf(pages=2)
    out = await PDFWatermark().add_watermark(src, "CONFIDENTIAL")
    reader = PdfReader(out)
    assert len(reader.pages) == 2
    assert "CONFIDENTIAL" in (reader.pages[0].extract_text() or "")


async def test_watermark_rejects_empty_text(make_pdf):
    src = make_pdf(pages=1)
    with pytest.raises(PDFProcessingError):
        await PDFWatermark().add_watermark(src, "")


async def test_password_add_and_remove_roundtrip(make_pdf):
    src = make_pdf(pages=1)
    protected = await PDFPassword().add_password(src, "secret123")
    assert PdfReader(protected).is_encrypted

    unprotected = await PDFPassword().remove_password(protected, "secret123")
    reader = PdfReader(unprotected)
    assert not reader.is_encrypted
    assert len(reader.pages) == 1


async def test_password_remove_rejects_wrong_password(make_pdf):
    src = make_pdf(pages=1)
    protected = await PDFPassword().add_password(src, "secret123")
    with pytest.raises(PDFProcessingError):
        await PDFPassword().remove_password(protected, "wrong")


async def test_image_to_pdf_combines_images(make_image):
    img1 = make_image("i1.png", mode="RGBA", color=(255, 0, 0, 128))
    img2 = make_image("i2.jpg", mode="RGB", color=(0, 255, 0))
    out = await ImageToPDF().convert([img1, img2])
    assert len(PdfReader(out).pages) == 2
