"""Add or remove a password on a PDF."""
import asyncio
from typing import Dict, Optional

from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions

from core.logger import logger
from utils.tempfiles import new_temp_path
from services.pdf._common import PDFProcessingError, open_pdf_reader

MIN_PASSWORD_LEN = 1
MAX_PASSWORD_LEN = 128

# Maps the Add Password tool's permission toggle keys (see
# bot.handlers.pdf.password) to the pypdf/PDF-spec access-permission bits
# they correspond to. Only these six are exposed to the user; anything not
# listed here (e.g. document assembly) is left at its default (allowed),
# matching the "No Restrictions" default state.
PERMISSION_FLAG_MAP: Dict[str, UserAccessPermissions] = {
    "print": UserAccessPermissions.PRINT,
    "copy": UserAccessPermissions.EXTRACT,
    "edit": UserAccessPermissions.MODIFY,
    "annotate": UserAccessPermissions.ADD_OR_MODIFY,
    "fill_forms": UserAccessPermissions.FILL_FORM_FIELDS,
    "accessibility": UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS,
}


def _validate_password_value(password: str) -> str:
    password = (password or "").strip()
    if len(password) < MIN_PASSWORD_LEN:
        raise PDFProcessingError("Password can't be empty.")
    if len(password) > MAX_PASSWORD_LEN:
        raise PDFProcessingError(f"Password is too long (max {MAX_PASSWORD_LEN} characters).")
    return password


def _build_permissions_flag(permissions: Optional[Dict[str, bool]]) -> Optional[UserAccessPermissions]:
    """Build a pypdf permissions bitmask from the tool's {key: bool} dict.
    Returns None (meaning "use pypdf's default", i.e. no restrictions) when
    `permissions` is None -- the "No Restrictions" case.
    """
    if permissions is None:
        return None
    flag = UserAccessPermissions(0)
    for key, allowed in permissions.items():
        bit = PERMISSION_FLAG_MAP.get(key)
        if bit is not None and allowed:
            flag |= bit
    return flag


class PDFPassword:
    async def add_password(
        self,
        input_path: str,
        password: str,
        permissions: Optional[Dict[str, bool]] = None,
    ) -> str:
        """Protect `input_path` with `password`.

        `permissions` is an optional {key: bool} dict (see
        PERMISSION_FLAG_MAP for the allowed keys) describing which actions
        should remain ALLOWED once the PDF is protected. Pass None for no
        restrictions (everything allowed, the tool's default).
        """
        password = _validate_password_value(password)
        return await asyncio.to_thread(self._add_password_sync, input_path, password, permissions)

    async def remove_password(self, input_path: str, password: str) -> str:
        password = _validate_password_value(password)
        return await asyncio.to_thread(self._remove_password_sync, input_path, password)

    async def verify_password(self, input_path: str, password: str) -> bool:
        """Check whether `password` unlocks the (already known to be
        encrypted) PDF at `input_path`, without writing anything out.
        Raises PDFProcessingError if the file can't be read at all.
        """
        password = _validate_password_value(password)
        return await asyncio.to_thread(self._verify_password_sync, input_path, password)

    async def change_password(self, input_path: str, old_password: str, new_password: str) -> str:
        """Replace `old_password` with `new_password` on `input_path`."""
        old_password = _validate_password_value(old_password)
        new_password = _validate_password_value(new_password)
        return await asyncio.to_thread(
            self._change_password_sync, input_path, old_password, new_password
        )

    @staticmethod
    def _add_password_sync(
        input_path: str,
        password: str,
        permissions: Optional[Dict[str, bool]],
    ) -> str:
        # open_pdf_reader() rejects already-encrypted PDFs by default, which
        # is what we want here: you must remove the old password first
        # rather than silently re-encrypting on top of it.
        reader = open_pdf_reader(input_path)
        writer = PdfWriter()
        try:
            # append() preserves page order, page size, and page
            # orientation as-is; explicitly carry over document metadata
            # too, since append() does not guarantee that on its own.
            writer.append(reader)
            if reader.metadata:
                try:
                    writer.add_metadata(reader.metadata)
                except Exception as e:
                    logger.debug(f"Add Password: could not preserve metadata: {e}")

            permissions_flag = _build_permissions_flag(permissions)
            if permissions_flag is None:
                writer.encrypt(password)
            else:
                writer.encrypt(password, permissions_flag=permissions_flag)

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

    @staticmethod
    def _verify_password_sync(input_path: str, password: str) -> bool:
        try:
            reader = PdfReader(input_path)
        except Exception as e:
            logger.exception(f"Could not read PDF for password verification: {e}")
            raise PDFProcessingError("Invalid or corrupted PDF.") from e

        if not reader.is_encrypted:
            raise PDFProcessingError("This PDF isn't password-protected.")

        try:
            result = reader.decrypt(password)
        except Exception as e:
            logger.debug(f"Password verification failed to decrypt: {e}")
            return False

        return bool(result)

    @staticmethod
    def _change_password_sync(input_path: str, old_password: str, new_password: str) -> str:
        try:
            reader = PdfReader(input_path)
        except Exception as e:
            logger.exception(f"Could not read PDF for password change: {e}")
            raise PDFProcessingError("Invalid or corrupted PDF.") from e

        if not reader.is_encrypted:
            raise PDFProcessingError("This PDF isn't password-protected.")

        try:
            result = reader.decrypt(old_password)
        except Exception as e:
            raise PDFProcessingError(f"Failed to decrypt PDF: {e}")

        if not result:
            raise PDFProcessingError("Incorrect password.")

        writer = PdfWriter()
        try:
            writer.append(reader)
            if reader.metadata:
                try:
                    writer.add_metadata(reader.metadata)
                except Exception as e:
                    logger.debug(f"Change Password: could not preserve metadata: {e}")
            writer.encrypt(new_password)
            output_path = new_temp_path(suffix=".pdf")
            with open(output_path, "wb") as f:
                writer.write(f)
            logger.info(f"Changed password protection on {input_path} -> {output_path}")
            return output_path
        finally:
            writer.close()
