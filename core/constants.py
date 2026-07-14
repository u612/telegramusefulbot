"""Application constants."""

# Menu labels
MAIN_MENU = "🏠 Main Menu"
HOME = "🏠 Home"
BACK = "⬅ Back"
CANCEL = "❌ Cancel"

# Callback data prefixes (to avoid 64-byte limit)
CB_PDF = "pdf"
CB_IMAGE = "img"
CB_ARCHIVE = "arc"
CB_DOCUMENT = "doc"
CB_OCR = "ocr"
CB_SETTINGS = "set"
CB_BACK = "back"
CB_HOME = "home"
CB_CANCEL = "cancel"

# NOTE: The runtime-configurable file size limit lives in core.config.settings
# (MAX_FILE_SIZE, driven by the MAX_FILE_SIZE env var). It used to also be
# hardcoded here, which meant two disagreeing sources of truth depending on
# which module a caller imported from. Removed on purpose -- always import
# the limit from `core.config.settings.MAX_FILE_SIZE`.

# FSM state-data key used to track every temp file/dir created during a flow.
# Every handler that writes a temp file MUST append its path to
# state.data[STATE_TEMP_FILES_KEY] (see utils.tempfiles.track_temp_file).
# base.py's back/home/cancel handlers -- and /start -- read this key to
# delete any leftover files before clearing FSM state, which is what fixes
# the "abandoned flow leaks temp files forever" bug.
STATE_TEMP_FILES_KEY = "temp_files"

# Supported extensions
SUPPORTED_PDF_EXTS = {".pdf"}
SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif"}
SUPPORTED_ARCHIVE_EXTS = {".zip", ".7z", ".rar"}
SUPPORTED_DOC_EXTS = {".docx", ".xlsx", ".pptx", ".txt", ".html", ".md"}

# Allowed MIME types (for validation)
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif",
    "application/zip", "application/x-7z-compressed", "application/x-rar-compressed",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain", "text/html", "text/markdown",
}
