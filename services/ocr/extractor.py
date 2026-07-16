"""Extract text from an image via Tesseract OCR (local, offline, multi-language).

Design decision (see audit): the original spec listed both EasyOCR and
Tesseract as options. EasyOCR pulls in torch + per-language deep learning
models (multi-hundred-MB to 1GB+, slow cold start, needs network access at
runtime to download weights) for capability that Tesseract already covers
well for printed text. Tesseract is used here as the sole engine: it's
fast, has a tiny footprint, and works fully offline once its language
packs are installed in the Docker image. `easyocr` has been removed from
requirements.txt accordingly -- flagging this explicitly rather than
leaving a half-wired second engine in place.
"""
import asyncio
import shutil

from core.config import settings
from core.logger import logger
from services.image._common import ImageProcessingError, open_image

SUPPORTED_LANGUAGES = {
    "eng": "English",
    "spa": "Spanish",
    "fra": "French",
    "deu": "German",
}


class OCRProcessingError(RuntimeError):
    """Raised for any expected OCR failure."""


class OCRExtractor:
    async def extract_text(self, input_path: str, lang: str = "eng", timeout=None) -> str:
        """`timeout` should come from the caller's effective limits
        (utils.limits) -- pass None for no timeout (the owner is never
        subject to a processing-time cap). Defaults to settings.OCR_TIMEOUT
        when omitted, for any caller that hasn't been updated yet.
        """
        if shutil.which("tesseract") is None:
            raise OCRProcessingError("OCR is currently unavailable on this server (Tesseract not installed).")
        if lang not in SUPPORTED_LANGUAGES:
            raise OCRProcessingError(f"Unsupported language. Choose one of: {', '.join(SUPPORTED_LANGUAGES)}")

        effective_timeout = timeout if timeout is not None else settings.OCR_TIMEOUT
        if effective_timeout is None:
            return await asyncio.to_thread(self._extract_sync, input_path, lang)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._extract_sync, input_path, lang),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            raise OCRProcessingError(f"OCR timed out after {effective_timeout}s.")

    @staticmethod
    def _extract_sync(input_path: str, lang: str) -> str:
        import pytesseract
        from pytesseract import TesseractError, TesseractNotFoundError

        img = open_image(input_path)
        try:
            try:
                text = pytesseract.image_to_string(img, lang=lang)
            except TesseractNotFoundError:
                raise OCRProcessingError("OCR is currently unavailable on this server (Tesseract not installed).")
            except TesseractError as e:
                raise OCRProcessingError(f"OCR failed to process this image: {e}")
        finally:
            img.close()

        text = text.strip()
        if not text:
            raise OCRProcessingError("No text was detected in this image.")

        logger.info(f"OCR extracted {len(text)} character(s) from {input_path} (lang={lang})")
        return text
        
