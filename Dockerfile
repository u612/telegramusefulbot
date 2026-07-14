# python:3.11 chosen over 3.13 deliberately: several heavy deps here
# (rembg's onnxruntime chain in particular) have historically lagged behind
# on new-CPython-version wheel availability, which turns into slow/broken
# source builds in CI. 3.11 has the widest, most stable wheel coverage.
FROM python:3.11-slim

# System dependencies:
#   tesseract-ocr + language packs -> services/ocr (OCR)
#   libreoffice                    -> services/document (DOCX/XLSX/PPTX/TXT/HTML/MD -> PDF)
#   ghostscript                    -> services/pdf/compressor.py
#   libmagic1                      -> utils/validators.py (MIME/magic-byte checks)
#   unar                           -> services/archive/extractor.py (RAR extraction fallback)
#   fonts-dejavu-core               -> baseline font coverage for LibreOffice + watermarking
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-spa \
    tesseract-ocr-fra \
    tesseract-ocr-deu \
    libreoffice \
    ghostscript \
    libmagic1 \
    unar \
    fonts-dejavu-core \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT=8000
ENV TEMP_FOLDER=/tmp/telegram_bot

# Run as a non-root user (defense in depth: a compromised dependency or
# a bug in file-processing code has no path to root on the container).
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /tmp/telegram_bot \
    && chown -R appuser:appuser /app /tmp/telegram_bot
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f "http://localhost:${PORT}/health" || exit 1

CMD ["python", "main.py"]
