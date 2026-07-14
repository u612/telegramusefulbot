"""Safely extract ZIP, 7Z, and RAR archives.

Every format funnels through the same two checks before writing any bytes:
1. `check_archive_bomb` on the declared (uncompressed size, file count,
   compression ratio) from the archive's own metadata.
2. `is_within_directory` + `safe_member_name` on every member path, to
   reject Zip Slip (`../../etc/passwd`-style) entries.

Note: RAR is extract-only. The `rarfile` library (and RAR's format itself,
without the proprietary WinRAR tool) cannot *create* RAR archives -- if you
need to "compress to RAR", that isn't offered anywhere in this bot; only
ZIP/7Z compression are.
"""
import asyncio
import os
import shutil
import zipfile
from typing import List

from core.logger import logger
from utils.tempfiles import new_temp_dir
from services.archive._common import ArchiveProcessingError, safe_member_name
from services.security.validator import is_within_directory, check_archive_bomb, ArchiveSecurityError

MAX_EXTRACTED_FILES_TO_RETURN = 20


class ArchiveExtractor:
    async def extract(self, input_path: str, archive_type: str) -> tuple:
        """Returns (dest_dir, file_list). `dest_dir` is the temp directory
        everything was extracted into -- deleting just that one path (via
        utils.tempfiles.delete_path, which shutil.rmtree's directories)
        cleans up every extracted file in one shot, rather than needing to
        track each nested file individually and leaving an empty directory
        tree behind.
        """
        archive_type = archive_type.lower()
        if archive_type not in ("zip", "7z", "rar"):
            raise ArchiveProcessingError(f"Unsupported archive type: {archive_type}")
        return await asyncio.to_thread(self._extract_sync, input_path, archive_type)

    @staticmethod
    def _extract_sync(input_path: str, archive_type: str) -> tuple:
        dest_dir = new_temp_dir(prefix="extract_")
        try:
            if archive_type == "zip":
                ArchiveExtractor._extract_zip(input_path, dest_dir)
            elif archive_type == "7z":
                ArchiveExtractor._extract_7z(input_path, dest_dir)
            else:
                ArchiveExtractor._extract_rar(input_path, dest_dir)

            extracted = []
            for root, _dirs, files in os.walk(dest_dir):
                for fn in files:
                    extracted.append(os.path.join(root, fn))

            if not extracted:
                raise ArchiveProcessingError("Archive is empty or contains no files.")

            logger.info(f"Extracted {len(extracted)} file(s) from {input_path} -> {dest_dir}")
            return dest_dir, sorted(extracted)
        except (ArchiveProcessingError, ArchiveSecurityError):
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise
        except Exception as e:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise ArchiveProcessingError(f"Failed to extract archive: {e}")

    @staticmethod
    def _extract_zip(input_path: str, dest_dir: str) -> None:
        try:
            zf = zipfile.ZipFile(input_path)
        except zipfile.BadZipFile as e:
            raise ArchiveProcessingError(f"Not a valid ZIP file: {e}")

        with zf:
            infolist = zf.infolist()
            check_archive_bomb(
                member_sizes=[i.file_size for i in infolist],
                compressed_size=os.path.getsize(input_path),
            )
            for info in infolist:
                if info.is_dir():
                    continue
                safe_name = safe_member_name(info.filename)
                target = os.path.join(dest_dir, safe_name)
                if not is_within_directory(dest_dir, target):
                    raise ArchiveSecurityError(f"Unsafe path in archive: {info.filename!r}")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    @staticmethod
    def _extract_7z(input_path: str, dest_dir: str) -> None:
        try:
            import py7zr
        except ImportError:
            raise ArchiveProcessingError("7Z support is currently unavailable on this server.")

        try:
            with py7zr.SevenZipFile(input_path, mode="r") as archive:
                entries = archive.list()
        except Exception as e:
            raise ArchiveProcessingError(f"Not a valid 7Z file: {e}")

        check_archive_bomb(
            member_sizes=[getattr(e, "uncompressed", 0) or 0 for e in entries],
            compressed_size=os.path.getsize(input_path),
        )
        for e in entries:
            if e.is_directory:
                continue
            safe_name = safe_member_name(e.filename)
            target = os.path.join(dest_dir, safe_name)
            if not is_within_directory(dest_dir, target):
                raise ArchiveSecurityError(f"Unsafe path in archive: {e.filename!r}")

        # py7zr's list() consumes the read position, so extraction needs a
        # fresh handle. extractall() is used here (names were already
        # validated above); py7zr writes relative to `path` using its own
        # internal names, which we've confirmed are all safe.
        with py7zr.SevenZipFile(input_path, mode="r") as archive:
            archive.extractall(path=dest_dir)

    @staticmethod
    def _extract_rar(input_path: str, dest_dir: str) -> None:
        try:
            import rarfile
        except ImportError:
            raise ArchiveProcessingError("RAR support is currently unavailable on this server.")

        if shutil.which("unrar") is None and shutil.which("unar") is None and shutil.which("bsdtar") is None:
            raise ArchiveProcessingError(
                "RAR extraction is currently unavailable on this server "
                "(no unrar/unar/bsdtar tool installed)."
            )

        try:
            rf = rarfile.RarFile(input_path)
        except rarfile.Error as e:
            raise ArchiveProcessingError(f"Not a valid RAR file: {e}")

        with rf:
            infolist = rf.infolist()
            check_archive_bomb(
                member_sizes=[i.file_size for i in infolist],
                compressed_size=os.path.getsize(input_path),
            )
            for info in infolist:
                if info.isdir():
                    continue
                safe_name = safe_member_name(info.filename)
                target = os.path.join(dest_dir, safe_name)
                if not is_within_directory(dest_dir, target):
                    raise ArchiveSecurityError(f"Unsafe path in archive: {info.filename!r}")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with rf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
