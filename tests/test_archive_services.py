"""Tests for services/archive/*, including real Zip Slip / bomb attack
simulations against the actual extraction code -- not just unit tests of
happy-path behavior.
"""
import os
import zipfile
import shutil

import pytest

from services.archive.compressor import ArchiveCompressor, ArchiveFormat
from services.archive.extractor import ArchiveExtractor
from services.security.validator import ArchiveSecurityError


async def test_zip_round_trip(tmp_path):
    f1 = tmp_path / "doc1.txt"
    f2 = tmp_path / "doc2.txt"
    f1.write_text("hello world")
    f2.write_text("second file contents")

    archive_path = await ArchiveCompressor().compress([str(f1), str(f2)], ArchiveFormat.ZIP)
    dest_dir, extracted = await ArchiveExtractor().extract(archive_path, "zip")
    try:
        assert len(extracted) == 2
        contents = sorted(open(p).read() for p in extracted)
        assert contents == sorted(["hello world", "second file contents"])
    finally:
        shutil.rmtree(dest_dir, ignore_errors=True)
        os.remove(archive_path)


async def test_zip_slip_relative_traversal_is_rejected(tmp_path):
    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../../../../tmp/pytest_evil_escaped_file.txt", "pwned")
        zf.writestr("normal.txt", "fine")

    escape_target = "/tmp/pytest_evil_escaped_file.txt"
    if os.path.exists(escape_target):
        os.remove(escape_target)  # clean slate in case a prior failing run left it

    with pytest.raises(ArchiveSecurityError):
        await ArchiveExtractor().extract(str(evil_zip), "zip")

    assert not os.path.exists(escape_target)


async def test_zip_slip_absolute_path_is_rejected(tmp_path):
    evil_zip = tmp_path / "evil2.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("/tmp/pytest_absolute_escape.txt", "pwned2")

    escape_target = "/tmp/pytest_absolute_escape.txt"
    if os.path.exists(escape_target):
        os.remove(escape_target)

    with pytest.raises(ArchiveSecurityError):
        await ArchiveExtractor().extract(str(evil_zip), "zip")

    assert not os.path.exists(escape_target)


async def test_legitimate_nested_subdirectory_still_works(tmp_path):
    nested_zip = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested_zip, "w") as zf:
        zf.writestr("sub/dir/file.txt", "nested content")

    dest_dir, extracted = await ArchiveExtractor().extract(str(nested_zip), "zip")
    try:
        assert len(extracted) == 1
        assert open(extracted[0]).read() == "nested content"
    finally:
        shutil.rmtree(dest_dir, ignore_errors=True)


async def test_rejects_excessive_file_count(tmp_path):
    bomb_zip = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb_zip, "w") as zf:
        for i in range(2100):
            zf.writestr(f"f{i}.txt", "x")

    with pytest.raises(ArchiveSecurityError):
        await ArchiveExtractor().extract(str(bomb_zip), "zip")
