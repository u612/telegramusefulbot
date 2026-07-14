"""Create a ZIP or 7Z archive from a set of input files."""
import asyncio
import os
import zipfile
from enum import Enum
from typing import List

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.archive._common import ArchiveProcessingError

MAX_FILES = 50


class ArchiveFormat(str, Enum):
    ZIP = "zip"
    SEVEN_Z = "7z"


class ArchiveCompressor:
    async def compress(self, input_paths: List[str], fmt: ArchiveFormat = ArchiveFormat.ZIP) -> str:
        if not input_paths:
            raise ArchiveProcessingError("No files provided.")
        if len(input_paths) > MAX_FILES:
            raise ArchiveProcessingError(f"Too many files (max {MAX_FILES}).")
        return await asyncio.to_thread(self._compress_sync, input_paths, fmt)

    @staticmethod
    def _compress_sync(input_paths: List[str], fmt: ArchiveFormat) -> str:
        if fmt == ArchiveFormat.ZIP:
            return ArchiveCompressor._zip_sync(input_paths)
        return ArchiveCompressor._sevenz_sync(input_paths)

    @staticmethod
    def _unique_arcnames(input_paths: List[str]) -> List[str]:
        """Use each file's original basename as the in-archive name, but
        de-duplicate if two uploads shared a filename.
        """
        seen = {}
        arcnames = []
        for path in input_paths:
            base = os.path.basename(path)
            count = seen.get(base, 0)
            seen[base] = count + 1
            arcnames.append(base if count == 0 else f"{count}_{base}")
        return arcnames

    @staticmethod
    def _zip_sync(input_paths: List[str]) -> str:
        output_path = new_temp_path(suffix=".zip")
        arcnames = ArchiveCompressor._unique_arcnames(input_paths)
        try:
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for path, arcname in zip(input_paths, arcnames):
                    zf.write(path, arcname=arcname)
            logger.info(f"Created ZIP with {len(input_paths)} file(s) -> {output_path}")
            return output_path
        except Exception as e:
            if os.path.exists(output_path):
                os.remove(output_path)
            raise ArchiveProcessingError(f"Failed to create ZIP archive: {e}")

    @staticmethod
    def _sevenz_sync(input_paths: List[str]) -> str:
        try:
            import py7zr
        except ImportError:
            raise ArchiveProcessingError("7Z support is currently unavailable on this server.")
        output_path = new_temp_path(suffix=".7z")
        arcnames = ArchiveCompressor._unique_arcnames(input_paths)
        try:
            with py7zr.SevenZipFile(output_path, "w") as zf:
                for path, arcname in zip(input_paths, arcnames):
                    zf.write(path, arcname=arcname)
            logger.info(f"Created 7Z with {len(input_paths)} file(s) -> {output_path}")
            return output_path
        except Exception as e:
            if os.path.exists(output_path):
                os.remove(output_path)
            raise ArchiveProcessingError(f"Failed to create 7Z archive: {e}")
          
