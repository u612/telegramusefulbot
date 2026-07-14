"""Convert DOCX/XLSX/PPTX/TXT/HTML/Markdown to PDF via headless LibreOffice.

Concurrency note: LibreOffice instances sharing the default user profile
lock each other out when run in parallel, causing conversions to silently
fail or hang under concurrent use. Every call here gets its own isolated
`-env:UserInstallation=` profile directory (created fresh and deleted
after), and a semaphore caps how many soffice processes run at once, since
each one is independently heavy regardless of profile isolation.
"""
import asyncio
import glob
import os
import shutil
import subprocess

from core.config import settings
from core.logger import logger
from utils.tempfiles import new_temp_dir, new_temp_path, delete_path

MAX_CONVERT_SECONDS_DEFAULT = 60


class DocumentProcessingError(RuntimeError):
    """Raised for any expected document conversion failure."""


_semaphore: "asyncio.Semaphore | None" = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_HEAVY_JOBS)
    return _semaphore


class DocumentConverter:
    async def convert_to_pdf(self, input_path: str) -> str:
        if shutil.which("soffice") is None:
            raise DocumentProcessingError(
                "Document conversion is currently unavailable on this server (LibreOffice not installed)."
            )

        async with _get_semaphore():
            return await asyncio.to_thread(self._convert_sync, input_path)

    @staticmethod
    def _convert_sync(input_path: str) -> str:
        profile_dir = new_temp_dir(prefix="lo_profile_")
        out_dir = new_temp_dir(prefix="lo_out_")
        try:
            cmd = [
                "soffice",
                "--headless",
                "--nologo",
                "--nofirststartwizard",
                "--norestore",
                f"-env:UserInstallation=file://{profile_dir}",
                "--convert-to", "pdf",
                "--outdir", out_dir,
                input_path,
            ]

            try:
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=settings.LIBREOFFICE_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                raise DocumentProcessingError(
                    f"Conversion timed out after {settings.LIBREOFFICE_TIMEOUT}s."
                )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace")[:500]
                raise DocumentProcessingError(f"Conversion failed: {stderr}")

            produced = glob.glob(os.path.join(out_dir, "*.pdf"))
            if not produced:
                raise DocumentProcessingError(
                    "Conversion did not produce a PDF (the file may be corrupted or password-protected)."
                )

            output_path = new_temp_path(suffix=".pdf")
            shutil.move(produced[0], output_path)
            logger.info(f"Converted {input_path} to PDF -> {output_path}")
            return output_path
        finally:
            delete_path(profile_dir)
            delete_path(out_dir)
          
