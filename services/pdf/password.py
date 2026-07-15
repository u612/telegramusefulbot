"""Add or remove a password on a PDF."""
import asyncio

from pypdf import PdfReader, PdfWriter

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.pdf._common import PDFProcessingError, open_pdf_reader

MIN_PASSWORD_LEN = 1
MAX_PASSWORD_LEN = 128


def _validate_password_value(password: str) -> str:
    password = (password or "").strip()
    if len(password) < MIN_PASSWORD_LEN:
        raise PDFProcessingError("Password can't be empty.")
    if len(password) > MAX_PASSWORD_LEN:
        raise PDFProcessingError(f"Password is too long (max {MAX_PASSWORD_LEN} characters).")
    return password


class PDFPassword:
    async def add_password(self, input_path: str, password: str) -> str:
        password = _validate_password_value(password)
        return await asyncio.to_thread(self._add_password_sync, input_path, password)

    async def remove_password(self, input_path: str, password: str) -> str:
        password = _validate_password_value(password)
        return await asyncio.to_thread(self._remove_password_sync, input_path, password)

    @staticmethod
    def _add_password_sync(input_path: str, password: str) -> str:
        # open_pdf_reader() rejects already-encrypted PDFs by default, which
        # is what we want here: you must remove the old password first
        # rather than silently re-encrypting on top of it.
        reader = open_pdf_reader(input_path)
        writer = PdfWriter()
        try:
            writer.append(reader)
            writer.encrypt(password)

            output_path = new_temp_path(suffix=".pdf")
            with open(output_path, "wb") as f:
                writer.write(f)
            logger.info(f"Added password protection to {input_path} -> {output_path}")
            return output_path
        finally:
            writer.close()

    @staticmethod
    def _remove_password_sync(input_path: str, password: str) -> str:
        try:
            reader = PdfReader(input_path)
        except Exception as e:
            logger.exception(f"Could not read PDF for password removal: {e}")
            raise PDFProcessingError("Invalid or corrupted PDF.") from e

        if not reader.is_encrypted:
            raise PDFProcessingError("This PDF isn't password-protected.")

        try:
            result = reader.decrypt(password)
        except Exception as e:
            raise PDFProcessingError(f"Failed to decrypt PDF: {e}")

        # pypdf's decrypt() returns a PasswordType-like value; 0/falsy means
        # the password was wrong.
        if not result:
            raise PDFProcessingError("Incorrect password.")

        writer = PdfWriter()
        try:
            writer.append(reader)
            output_path = new_temp_path(suffix=".pdf")
            with open(output_path, "wb") as f:
                writer.write(f)
            logger.info(f"Removed password protection from {input_path} -> {output_path}")
            return output_path
        finally:
            writer.close()
