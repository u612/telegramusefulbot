# Telegram File Toolkit Bot

A Telegram bot for PDF, image, archive, and document processing, plus OCR,
built on aiogram 3 + FastAPI, deployable to Railway.

## Features

| Category | Operations |
|---|---|
| PDF | Merge, Split, Compress, Rotate, Extract pages, Rearrange pages, Watermark, Add/Remove password, Image→PDF, PDF→Images |
| Images | Compress, Resize, Crop, Rotate, Flip, Convert format, Remove background, Watermark, Remove metadata |
| Archives | Compress (ZIP/7Z), Extract (ZIP/7Z/RAR) |
| Documents | Convert DOCX/XLSX/PPTX/TXT/HTML/Markdown → PDF (via LibreOffice) |
| OCR | Extract text from images (English/Spanish/French/German) via Tesseract |

**Scope notes (deliberate, not oversights):**
- **RAR is extract-only.** Neither `rarfile` nor the RAR format itself
  (without the proprietary WinRAR tool) can *create* RAR archives, so
  "compress to RAR" isn't offered anywhere — only ZIP/7Z compression are.
- **OCR uses Tesseract only**, not EasyOCR. EasyOCR pulls in torch and
  per-language deep-learning models (several hundred MB to 1GB+, slow cold
  start, needs network access at runtime to fetch weights) for accuracy
  gains Tesseract's printed-text recognition mostly already covers.
  Tesseract is fast, has a tiny footprint, and works fully offline once
  its language packs are installed (see Dockerfile).

## Architecture

```
main.py                  FastAPI app + aiogram polling task, wired together
core/                     config (env-driven settings), constants, logging
bot/
  handlers/               one file per feature area; routers registered in main.py
  keyboards/               inline keyboards
  states/                  aiogram FSM state groups
  middlewares/             logging, DB session/user, throttling
database/                 SQLAlchemy async models + repository pattern
services/
  pdf/, image/, archive/, document/, ocr/     the actual processing logic
  security/                shared validation: safe subprocess exec,
                            Zip Slip / Zip Bomb protection, startup checks
utils/                     temp-file lifecycle, validators, page-range parsing
```

### Temp file lifecycle (important if you're extending this)

Every handler that downloads a file into a temp path calls
`track_temp_file(state, path)` immediately after writing it. `base.py`'s
Back/Home/Cancel handlers (and `/start`) call `cleanup_tracked_files(state)`
before clearing FSM state, which deletes anything still tracked. This is
what prevents an abandoned multi-step flow (e.g. a user uploads 3 files to
Merge, then presses Cancel) from leaking files on disk forever — see
`tests/test_utils.py::test_abandoned_flow_cleans_up_tracked_files` for the
regression test.

When you add a new handler: download to `utils.tempfiles.new_temp_path()`,
call `await track_temp_file(state, path)` right after, and on both success
and failure paths call `delete_paths([...])` + `await
untrack_temp_files(state, [...])` (see any existing handler in
`bot/handlers/pdf.py` for the pattern).

### Security

- **MIME/magic-byte validation** (`utils/validators.py`) runs on every
  upload, not just the filename extension.
- **Zip Slip / Zip Bomb protection** (`services/security/validator.py`,
  used by `services/archive/extractor.py`): every archive member's path is
  validated and *rejected* (not silently sanitized) if it contains `..`
  traversal or an absolute path, and each archive's declared file count /
  uncompressed size / compression ratio is checked against limits before
  any bytes are extracted. See `tests/test_archive_services.py` for actual
  attack-simulation tests.
- **No shell=True anywhere.** All subprocess calls (Ghostscript,
  LibreOffice) use argv lists via `asyncio.create_subprocess_exec` /
  `subprocess.run`, never a shell string.
- Runs as a **non-root user** in the Docker image.

## Environment variables

See `.env.example`. `BOT_TOKEN` and `DATABASE_URL` are required; everything
else has a sensible default.

## Local development

```bash
cp .env.example .env   # fill in BOT_TOKEN and DATABASE_URL
pip install -r requirements-dev.txt
python main.py
```

Or with Docker Compose (spins up Postgres too):

```bash
docker-compose up --build
```

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests exercise the real processing libraries (pypdf, reportlab, Pillow,
zipfile) against actual generated PDFs/images/archives — including a real
Zip Slip attack simulation — not mocks.

## Deployment on Railway

1. Push this repo, create a Railway project from it (Dockerfile is
   auto-detected).
2. Add a PostgreSQL plugin, or point `DATABASE_URL` at your own instance.
3. Set environment variables: `BOT_TOKEN` (required), `DATABASE_URL`
   (required), and optionally any of the tunables in `.env.example`.
   **Do not set `PORT`** — Railway injects it automatically and the app
   reads it from the environment.
4. Deploy. The bot uses long polling (not webhooks), so no public URL
   configuration is needed beyond Railway's own health check hitting
   `GET /health`.

## Known limitations

- FSM state is in-memory (`MemoryStorage`), so a redeploy mid-flow drops
  any in-progress multi-step operation (the user just starts over) and the
  bot only supports a single replica — `REDIS_URL` is reserved in config
  for a future `RedisStorage` swap but isn't wired up yet.
- LibreOffice document conversion and OCR are CPU-heavy; a
  `MAX_CONCURRENT_HEAVY_JOBS` semaphore (default 2) caps how many run at
  once per process, but this is a single-process limit, not a
  cluster-wide one.
- Background removal (`rembg`) downloads its model on first use, which
  requires outbound network access from the container at runtime.
