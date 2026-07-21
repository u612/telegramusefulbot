"""Image -> PDF: choose a page size, then upload one or more images (as
Telegram photos, albums, or image documents) and build a single PDF, in
exact upload order. Mirrors Merge's buffer -> debounce -> download-in-
order -> commit architecture (see merge.py) so bursts/albums are handled
identically across the toolkit.

PDF -> Images: choose an output format first, then upload the PDF.
"""
import asyncio
import time
from typing import Dict, List, Optional

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, InputMediaPhoto, InputMediaDocument,
)
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, UnidentifiedImageError

from bot.states.pdf import PDFStates
from bot.keyboards.pdf import (
    PDF_IMAGE_TO_PDF, PDF_PDF_TO_IMAGES, PDF_DONE,
    get_pdf_menu,
    PDF_TO_IMG_PNG, PDF_TO_IMG_JPEG,
)
from bot.keyboards.common import back_home_cancel
from core.constants import SUPPORTED_PDF_EXTS, MERGE_BATCH_FINALIZE_DELAY_SECONDS
from core.logger import logger
from utils.limits import get_effective_limits
from utils.session_manager import register_stale_callbacks

from services.pdf.image_to_pdf import ImageToPDF
from services.pdf.pdf_to_images import (
    PDFToImages, DPI_STANDARD, DPI_HIGH, DPI_MAXIMUM,
)
from services.pdf._common import PDFProcessingError

from utils.tempfiles import (
    new_temp_path, track_temp_file, untrack_temp_files,
    get_tracked_files, delete_paths,
)
from utils.validators import validate_extension, validate_file_size, sanitize_filename

from .common import (
    _PDF_MIME, _download_and_validate, _fail, _track_usage,
    _send_temp_validation_error,
)

router = Router()

# --------------------------------------------------------------------------
# Image -> PDF
# --------------------------------------------------------------------------

_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"

# Image to PDF accepts a narrower set than the toolkit's general image
# tools -- explicitly NOT gif/animation, per spec.
_IMG2PDF_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
_IMG2PDF_MIMES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff"}

PAGE_SIZE_A4 = "A4"
PAGE_SIZE_LETTER = "LETTER"
PAGE_SIZE_ORIGINAL = "ORIGINAL"
_PAGE_SIZE_LABELS = {PAGE_SIZE_A4: "A4", PAGE_SIZE_LETTER: "Letter", PAGE_SIZE_ORIGINAL: "Original Size"}

IMG2PDF_CB_PAGESIZE_A4 = "img2pdf:size:a4"
IMG2PDF_CB_PAGESIZE_LETTER = "img2pdf:size:letter"
IMG2PDF_CB_PAGESIZE_ORIGINAL = "img2pdf:size:original"
IMG2PDF_CB_REVERSE = "img2pdf:reverse"
IMG2PDF_CB_RESTORE_ORDER = "img2pdf:restore_order"
IMG2PDF_CB_CLEAR = "img2pdf:clear"
IMG2PDF_CB_CLEAR_YES = "img2pdf:clear_yes"
IMG2PDF_CB_CLEAR_NO = "img2pdf:clear_no"
IMG2PDF_CB_CREATE = "img2pdf:create"
IMG2PDF_CB_CANCEL = "img2pdf:cancel"
IMG2PDF_CB_CANCEL_YES = "img2pdf:cancel_yes"
IMG2PDF_CB_CANCEL_NO = "img2pdf:cancel_no"


class _Img2PdfBatch:
    """Per-chat, in-memory state for one Image->PDF flow's not-yet-
    committed uploads. Deliberately NOT stored in FSM data -- it holds
    live aiogram Message objects and an asyncio.Task. Mirrors Merge's
    `_MergeBatch` exactly; see that class's docstring for the full
    rationale (single-process bot + aiogram MemoryStorage assumption).
    """

    __slots__ = ("pending", "seen_file_unique_ids", "task")

    def __init__(self):
        # Messages buffered since the last finalize, in the order the
        # handler received them. Arrival order is NOT the final upload
        # order for albums -- see `_order_pending_for_download`, which
        # re-sorts by media_group_id + message_id before downloading.
        self.pending: List[Message] = []
        self.seen_file_unique_ids: set = set()
        self.task: Optional[asyncio.Task] = None


_img2pdf_batches: Dict[int, _Img2PdfBatch] = {}
_img2pdf_locks: Dict[int, asyncio.Lock] = {}


def _get_img2pdf_batch(chat_id: int) -> _Img2PdfBatch:
    batch = _img2pdf_batches.get(chat_id)
    if batch is None:
        batch = _Img2PdfBatch()
        _img2pdf_batches[chat_id] = batch
    return batch


def get_img2pdf_lock(chat_id: int) -> asyncio.Lock:
    """Single per-chat lock serializing everything that touches a chat's
    Image->PDF upload queue -- see `get_merge_lock`'s docstring for why
    this pattern matters (buffering vs. Create PDF vs. Cancel racing).
    """
    lock = _img2pdf_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _img2pdf_locks[chat_id] = lock
    return lock


def cancel_pending_img2pdf_batch(chat_id: int) -> None:
    """Cancel any pending Image->PDF batch-finalize task, discard any
    buffered (not-yet-downloaded) messages, and drop this chat's lock.
    Called whenever the flow leaves the "waiting for images" stage --
    Create, Cancel, Clear All, Back, Home, or a fresh /start.
    """
    batch = _img2pdf_batches.pop(chat_id, None)
    if batch and batch.task and not batch.task.done():
        batch.task.cancel()
    _img2pdf_locks.pop(chat_id, None)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _page_size_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📄 A4", callback_data=IMG2PDF_CB_PAGESIZE_A4)
    b.button(text="📃 Letter", callback_data=IMG2PDF_CB_PAGESIZE_LETTER)
    b.button(text="🖼 Original Size", callback_data=IMG2PDF_CB_PAGESIZE_ORIGINAL)
    b.button(text="❌ Cancel", callback_data=IMG2PDF_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _upload_prompt_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=IMG2PDF_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _images_added_keyboard(reversed_: bool):
    b = InlineKeyboardBuilder()
    if reversed_:
        b.button(text="↩ Restore Upload Order", callback_data=IMG2PDF_CB_RESTORE_ORDER)
    else:
        b.button(text="🔁 Reverse Order", callback_data=IMG2PDF_CB_REVERSE)
    b.button(text="🗑 Clear All", callback_data=IMG2PDF_CB_CLEAR)
    b.button(text="📄 Create PDF", callback_data=IMG2PDF_CB_CREATE)
    b.button(text="❌ Cancel", callback_data=IMG2PDF_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _filename_prompt_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=IMG2PDF_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _clear_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Yes, Clear All", callback_data=IMG2PDF_CB_CLEAR_YES)
    b.button(text="↩ Keep Images", callback_data=IMG2PDF_CB_CLEAR_NO)
    b.adjust(2)
    return b.as_markup()


def _cancel_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yes, Cancel", callback_data=IMG2PDF_CB_CANCEL_YES)
    b.button(text="↩ Continue", callback_data=IMG2PDF_CB_CANCEL_NO)
    b.adjust(2)
    return b.as_markup()


def _render_welcome_text() -> str:
    return (
        "📷 Image to PDF\n\n"
        "Convert one or more images into a PDF.\n\n"
        "Images will appear in the PDF in the exact order you send them.\n\n"
        "Choose your preferred page size."
    )


def _render_upload_prompt_text(page_size: str) -> str:
    return (
        "📷 Image to PDF\n\n"
        f"📄 Page Size\n\n{_PAGE_SIZE_LABELS[page_size]}\n\n"
        "Send one or more images.\n\n"
        "Supported:\n\n"
        "• Photos\n"
        "• Albums\n"
        "• Image Documents\n\n"
        "Images will appear in the PDF in the exact order you send them.\n\n"
        "Upload your images to begin."
    )


def _render_images_added_text(count: int, page_size: str, reversed_: bool) -> str:
    lines = [
        "📷 Images Added\n",
        f"🖼 Images\n\n{count}\n",
        f"📄 Page Size\n\n{_PAGE_SIZE_LABELS[page_size]}\n",
    ]
    if reversed_:
        lines.append("✅ Images will now appear in reverse upload order.\n")
    else:
        lines.append("Images will appear in the PDF in the exact order you uploaded them.\n")
    lines.append("Send more images to continue, or create your PDF when you're ready.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Workflow message helpers (delete-then-send / edit-in-place)
# --------------------------------------------------------------------------

async def _send_new_workflow_message(bot, state: FSMContext, chat_id: int, text: str, keyboard) -> None:
    """Delete the previous workflow message (if any) and send a fresh one
    below whatever was most recently uploaded, keeping controls at the
    bottom of the chat.
    """
    data = await state.get_data()
    old_chat_id = data.get("img2pdf_status_chat_id")
    old_message_id = data.get("img2pdf_status_message_id")
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Image->PDF: status delete skipped: {e}")

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await state.update_data(img2pdf_status_chat_id=chat_id, img2pdf_status_message_id=sent.message_id)


async def _edit_workflow_message(bot, state: FSMContext, text: str, keyboard) -> None:
    data = await state.get_data()
    chat_id = data.get("img2pdf_status_chat_id")
    message_id = data.get("img2pdf_status_message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
    except Exception as e:
        logger.debug(f"Image->PDF: status edit skipped: {e}")


def _current_order(data: dict) -> List[str]:
    upload_order: List[str] = list(data.get("img2pdf_upload_order", []))
    if data.get("img2pdf_reversed"):
        return list(reversed(upload_order))
    return upload_order


async def _show_images_added_screen(bot, state: FSMContext, chat_id: int, as_new_message: bool) -> None:
    data = await state.get_data()
    order = _current_order(data)
    page_size = data.get("img2pdf_page_size", PAGE_SIZE_A4)
    reversed_ = bool(data.get("img2pdf_reversed"))
    text = _render_images_added_text(len(order), page_size, reversed_)
    keyboard = _images_added_keyboard(reversed_)
    if as_new_message:
        await _send_new_workflow_message(bot, state, chat_id, text, keyboard)
    else:
        await _edit_workflow_message(bot, state, text, keyboard)


# --------------------------------------------------------------------------
# Step 1 -- welcome / page size
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_IMAGE_TO_PDF)
async def pdf_image_to_pdf_start(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    cancel_pending_img2pdf_batch(chat_id)  # defensive: no stale buffer from a previous flow
    await state.set_state(PDFStates.waiting_for_page_size_img2pdf)
    await query.message.edit_text(_render_welcome_text(), reply_markup=_page_size_keyboard())
    await state.update_data(
        img2pdf_status_chat_id=chat_id,
        img2pdf_status_message_id=query.message.message_id,
        img2pdf_upload_order=[],
        img2pdf_reversed=False,
        img2pdf_page_size=None,
        img2pdf_creating=False,
    )
    await query.answer()


@router.callback_query(
    PDFStates.waiting_for_page_size_img2pdf,
    F.data.in_({IMG2PDF_CB_PAGESIZE_A4, IMG2PDF_CB_PAGESIZE_LETTER, IMG2PDF_CB_PAGESIZE_ORIGINAL}),
)
async def pdf_image_to_pdf_page_size_chosen(query: CallbackQuery, state: FSMContext):
    page_size = {
        IMG2PDF_CB_PAGESIZE_A4: PAGE_SIZE_A4,
        IMG2PDF_CB_PAGESIZE_LETTER: PAGE_SIZE_LETTER,
        IMG2PDF_CB_PAGESIZE_ORIGINAL: PAGE_SIZE_ORIGINAL,
    }[query.data]
    await state.update_data(img2pdf_page_size=page_size)
    await state.set_state(PDFStates.waiting_for_images_to_pdf)
    await query.message.edit_text(_render_upload_prompt_text(page_size), reply_markup=_upload_prompt_keyboard())
    await query.answer()


# --------------------------------------------------------------------------
# Upload buffering (mirrors Merge's buffer -> debounce -> download-in-
# order -> commit; see merge.py's _process_pending_batch /
# _finalize_merge_batch for the full rationale).
# --------------------------------------------------------------------------

async def _download_one_image(message: Message, state: FSMContext, size_ceiling: Optional[int]) -> Optional[str]:
    """Download + validate a single buffered message (photo or image
    document). Returns the temp path on success, None on any failure
    (corrupt/unreadable/oversized/unsupported) -- caller reports failures
    without losing track of the rest of the batch.
    """
    if message.photo:
        photo = message.photo[-1]  # largest size Telegram kept
        path = new_temp_path(suffix=".jpg")
        await track_temp_file(state, path)
        try:
            await message.bot.download(photo, destination=path)
        except Exception as e:
            logger.error(f"Image->PDF: photo download failed: {e}")
            await untrack_temp_files(state, [path])
            delete_paths([path])
            return None
    else:
        doc = message.document
        suffix = "." + (doc.file_name or "").rsplit(".", 1)[-1].lower() if "." in (doc.file_name or "") else ""
        path = new_temp_path(suffix=suffix)
        await track_temp_file(state, path)
        try:
            await message.bot.download(doc, destination=path)
        except Exception as e:
            logger.error(f"Image->PDF: document download failed: {e}")
            await untrack_temp_files(state, [path])
            delete_paths([path])
            return None
        if size_ceiling is not None and not validate_file_size(path, size_ceiling):
            await untrack_temp_files(state, [path])
            delete_paths([path])
            return None

    # Verify the file actually decodes as an image (catches corrupted /
    # unreadable files and decoding failures per spec).
    try:
        with Image.open(path) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, ValueError) as e:
        logger.info(f"Image->PDF: rejected undecodable image: {e}")
        await untrack_temp_files(state, [path])
        delete_paths([path])
        return None

    return path


def _order_pending_for_download(pending: List[Message]) -> List[Message]:
    """Return `pending` reordered so it matches the order the user actually
    sees in Telegram, not the (unreliable) order the handler received the
    updates in.

    Telegram delivers each photo/document of an album as a separate
    update, and those updates can arrive out of order. Grouping by
    `media_group_id` and sorting each group by `message_id` recovers the
    true album order, since Telegram assigns message_ids sequentially in
    the order the album was sent. Standalone messages (no media_group_id)
    are treated as their own single-item group, so they simply keep their
    relative position -- the position a key is used for the first time is
    its position in the output, which for a solo message is just where it
    arrived among the other bursts/groups.
    """
    groups: "Dict[object, List[Message]]" = {}
    key_order: List[object] = []
    for msg in pending:
        key = msg.media_group_id if msg.media_group_id else ("__solo__", msg.message_id)
        if key not in groups:
            groups[key] = []
            key_order.append(key)
        groups[key].append(msg)

    ordered: List[Message] = []
    for key in key_order:
        group = groups[key]
        group.sort(key=lambda m: m.message_id)
        ordered.extend(group)
    return ordered


async def _process_pending_img2pdf_batch(state: FSMContext, chat_id: int, limits, size_ceiling: Optional[int]) -> tuple:
    """MUST be called while holding get_img2pdf_lock(chat_id). Downloads
    every currently-buffered message, in the user's true upload order
    (see `_order_pending_for_download`), appending each to the session's
    upload order before moving to the next. Returns (added_count, failed_count).
    """
    batch = _img2pdf_batches.get(chat_id)
    if batch is None or not batch.pending:
        return 0, 0

    pending = _order_pending_for_download(batch.pending)
    batch.pending = []

    added = 0
    failed = 0
    for msg in pending:
        data = await state.get_data()
        order: List[str] = list(data.get("img2pdf_upload_order", []))
        if limits is not None and not limits.unlimited and len(order) >= limits.image_to_pdf_limit:
            failed += 1
            continue
        path = await _download_one_image(msg, state, size_ceiling)
        if path is None:
            failed += 1
            continue
        order.append(path)
        await state.update_data(img2pdf_upload_order=order)
        added += 1

    return added, failed


async def _finalize_img2pdf_batch(bot, state: FSMContext, chat_id: int, limits, size_ceiling: Optional[int]) -> None:
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    async with get_img2pdf_lock(chat_id):
        batch = _img2pdf_batches.get(chat_id)
        if batch is None or batch.task is not asyncio.current_task():
            return
        batch.task = None

        if await state.get_state() != PDFStates.waiting_for_images_to_pdf.state:
            return  # flow was cancelled/finished/navigated away meanwhile

        added, failed = await _process_pending_img2pdf_batch(state, chat_id, limits, size_ceiling)
        if added == 0 and failed == 0:
            return

        if failed:
            await _send_temp_validation_error(
                bot, chat_id,
                f"⚠️ Skipped {failed} file(s) that couldn't be added (unsupported, corrupted, or over the limit).",
            )

        data = await state.get_data()
        if not data.get("img2pdf_upload_order"):
            # Everything in this burst failed -- the "Processing..." message
            # is already on screen, so edit it back to the upload prompt
            # instead of leaving it stuck, or sending a duplicate message.
            page_size = data.get("img2pdf_page_size", PAGE_SIZE_A4)
            await _edit_workflow_message(
                bot, state, _render_upload_prompt_text(page_size), _upload_prompt_keyboard()
            )
            return

        # The "Processing..." message sent when this burst started is
        # still on screen below the uploads -- edit it into the final
        # "Images Added" screen rather than sending a new message.
        await _show_images_added_screen(bot, state, chat_id, as_new_message=False)
        logger.info(f"Image->PDF: batch finalized for chat {chat_id}: {added} added, {failed} failed")


@router.message(PDFStates.waiting_for_images_to_pdf, F.photo | F.document)
async def pdf_image_to_pdf_receive(message: Message, state: FSMContext, db_user=None):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if message.document is not None:
        if not validate_extension(message.document.file_name or "", _IMG2PDF_EXTS):
            try:
                await message.delete()
            except Exception as e:
                logger.debug(f"Image->PDF: could not delete rejected document: {e}")
            await _send_temp_validation_error(
                message.bot, chat_id,
                "❌ Unsupported file type. Please send JPG, PNG, WEBP, BMP, or TIFF images only.",
            )
            return

    limits = get_effective_limits(user_id, db_user)
    from core.config import settings
    size_ceiling = None if limits.unlimited else settings.MAX_FILE_SIZE

    lock = get_img2pdf_lock(chat_id)
    async with lock:
        batch = _get_img2pdf_batch(chat_id)

        file_unique_id = (
            message.photo[-1].file_unique_id if message.photo else message.document.file_unique_id
        )
        if file_unique_id and file_unique_id in batch.seen_file_unique_ids:
            logger.info(f"Image->PDF: duplicate upload ignored for user {user_id}")
            return

        data_now = await state.get_data()
        total_so_far = len(data_now.get("img2pdf_upload_order", [])) + len(batch.pending)
        # `total_so_far` counts both already-committed uploads and anything
        # still buffered in this burst, so the limit check below can never
        # be bypassed by a fast follow-up burst racing the debounce timer.
        if not limits.unlimited and total_so_far >= limits.image_to_pdf_limit:
            await _send_temp_validation_error(
                message.bot, chat_id,
                f"⚠️ Maximum of {limits.image_to_pdf_limit} images reached.",
            )
            return

        if file_unique_id:
            batch.seen_file_unique_ids.add(file_unique_id)

        starting_new_burst = batch.task is None
        batch.pending.append(message)

        previous_task = batch.task
        if previous_task and not previous_task.done():
            previous_task.cancel()

        if starting_new_burst:
            # Show feedback immediately -- don't make the user wait on
            # downloads/validation before they see anything happened.
            # This same message is later edited in place once processing
            # finishes (see `_finalize_img2pdf_batch`).
            await _send_new_workflow_message(
                message.bot, state, chat_id,
                "⏳ Processing uploaded images...\n\nPlease wait while your images are being prepared.",
                None,
            )

        batch.task = asyncio.create_task(
            _finalize_img2pdf_batch(message.bot, state, chat_id, limits, size_ceiling)
        )


@router.message(PDFStates.waiting_for_images_to_pdf)
async def pdf_image_to_pdf_reject_wrong_input(message: Message):
    """Everything that isn't a photo/document reaches here (video, audio,
    voice, sticker, animation/GIF, text). Deleted + a temporary validation
    error, same treatment as every other tool in the toolkit.
    """
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Image->PDF: could not delete invalid upload: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id,
        "❌ Please send images only (Photos, Albums, or Image Documents).",
    )


# --------------------------------------------------------------------------
# Reverse Order / Restore Upload Order
# --------------------------------------------------------------------------

@router.callback_query(PDFStates.waiting_for_images_to_pdf, F.data == IMG2PDF_CB_REVERSE)
async def pdf_image_to_pdf_reverse(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(img2pdf_reversed=True)
    await _show_images_added_screen(query.bot, state, query.message.chat.id, as_new_message=False)


@router.callback_query(PDFStates.waiting_for_images_to_pdf, F.data == IMG2PDF_CB_RESTORE_ORDER)
async def pdf_image_to_pdf_restore_order(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(img2pdf_reversed=False)
    await _show_images_added_screen(query.bot, state, query.message.chat.id, as_new_message=False)


# --------------------------------------------------------------------------
# Clear All
# --------------------------------------------------------------------------

@router.callback_query(PDFStates.waiting_for_images_to_pdf, F.data == IMG2PDF_CB_CLEAR)
async def pdf_image_to_pdf_clear_ask(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _edit_workflow_message(
        query.bot, state,
        "⚠️ Remove all uploaded images?\n\nThis action cannot be undone.",
        _clear_confirm_keyboard(),
    )


@router.callback_query(F.data == IMG2PDF_CB_CLEAR_YES)
async def pdf_image_to_pdf_clear_yes(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    await query.answer()
    async with get_img2pdf_lock(chat_id):
        cancel_pending_img2pdf_batch(chat_id)
        files = await get_tracked_files(state)
        if files:
            delete_paths(files)
            await untrack_temp_files(state, files)
        page_size = (await state.get_data()).get("img2pdf_page_size", PAGE_SIZE_A4)
        await state.update_data(img2pdf_upload_order=[], img2pdf_reversed=False)

    try:
        await query.message.delete()
    except Exception as e:
        logger.debug(f"Image->PDF: could not delete workflow message after Clear All: {e}")

    sent = await query.bot.send_message(
        chat_id, _render_upload_prompt_text(page_size), reply_markup=_upload_prompt_keyboard()
    )
    await state.update_data(img2pdf_status_chat_id=chat_id, img2pdf_status_message_id=sent.message_id)


@router.callback_query(F.data == IMG2PDF_CB_CLEAR_NO)
async def pdf_image_to_pdf_clear_no(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _show_images_added_screen(query.bot, state, query.message.chat.id, as_new_message=False)


# --------------------------------------------------------------------------
# Create PDF
# --------------------------------------------------------------------------

@router.callback_query(PDFStates.waiting_for_images_to_pdf, F.data == IMG2PDF_CB_CREATE)
async def pdf_image_to_pdf_create(query: CallbackQuery, state: FSMContext):
    """Mirrors Merge's Confirm -> filename prompt step: don't generate yet,
    just ask for the output filename first (see
    `pdf_merge_preview_confirm` in merge.py).
    """
    chat_id = query.message.chat.id
    await query.answer()

    async with get_img2pdf_lock(chat_id):
        data = await state.get_data()
        if data.get("img2pdf_creating"):
            return  # duplicate press while already processing
        order = _current_order(data)
        if not order:
            await _send_temp_validation_error(query.bot, chat_id, "⚠️ Please upload at least one image.")
            return

        cancel_pending_img2pdf_batch(chat_id)
        await state.set_state(PDFStates.waiting_for_img2pdf_filename)
        text = (
            f"🖼 Images Added: {len(order)}\n\n"
            "📝 Send the output filename (e.g. my_images) -- I'll add .pdf for you."
        )
        await _edit_workflow_message(query.bot, state, text, _filename_prompt_keyboard())
        logger.info(f"Image->PDF: awaiting output filename from user {query.from_user.id}")


@router.message(PDFStates.waiting_for_img2pdf_filename, F.text)
async def pdf_image_to_pdf_filename_receive(message: Message, state: FSMContext, user_repo=None, db_user=None):
    chat_id = message.chat.id

    async with get_img2pdf_lock(chat_id):
        data = await state.get_data()
        if data.get("img2pdf_creating"):
            return  # duplicate submission while already processing
        order = _current_order(data)
        if not order:
            await message.answer("Session expired, please start over.")
            await state.clear()
            return

        # Same sanitization/fallback Merge uses for a user-supplied name.
        safe = sanitize_filename(message.text.strip())
        if safe.lower().endswith(".pdf"):
            safe = safe[:-4]
        if not safe:
            safe = "images"
        filename = f"{safe}.pdf"

        await state.update_data(img2pdf_creating=True)

        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"Image->PDF: could not delete user's filename message: {e}")

        await _edit_workflow_message(
            message.bot, state,
            "⏳ Creating your PDF...\n\nPlease wait while your PDF is being generated.",
            None,
        )

        page_size = data.get("img2pdf_page_size", PAGE_SIZE_A4)
        limits = get_effective_limits(message.from_user.id, db_user)
        try:
            output_path = await ImageToPDF().convert(
                order,
                max_images=limits.image_to_pdf_limit,
                page_size=None if page_size == PAGE_SIZE_ORIGINAL else page_size,
            )
        except Exception as e:
            await _fail(message, state, e, order)
            return

        await track_temp_file(state, output_path)
        cleanup_paths = order + [output_path]
        count = len(order)
        status_data = await state.get_data()
        status_chat_id = status_data.get("img2pdf_status_chat_id")
        status_message_id = status_data.get("img2pdf_status_message_id")
        try:
            await message.bot.send_document(chat_id, FSInputFile(output_path, filename=filename))
            await _track_usage(user_repo, db_user)
            if status_chat_id is not None and status_message_id is not None:
                try:
                    await message.bot.delete_message(chat_id=status_chat_id, message_id=status_message_id)
                except Exception as e:
                    logger.debug(f"Image->PDF: could not delete 'Creating your PDF...' message: {e}")
            await message.bot.send_message(
                chat_id,
                "✅ PDF created successfully!\n\n"
                f"🖼 Images\n\n{count}\n\n"
                f"📄 Page Size\n\n{_PAGE_SIZE_LABELS[page_size]}",
            )
        except PDFProcessingError as e:
            await message.bot.send_message(chat_id, f"⚠️ {e}")
        except Exception:
            logger.exception(f"Image->PDF: failed to send output to user {message.from_user.id}")
            await message.bot.send_message(chat_id, "Something went wrong sending your PDF. Please try again.")
        finally:
            delete_paths(cleanup_paths)
            await untrack_temp_files(state, cleanup_paths)
            await state.clear()
            logger.info(f"Image->PDF: cleanup done for user {message.from_user.id} ({len(cleanup_paths)} temp paths)")


@router.message(PDFStates.waiting_for_img2pdf_filename)
async def pdf_image_to_pdf_filename_wrong_input(message: Message):
    """Catches non-text input while waiting for the output filename. Same
    treatment as every other invalid input in the toolkit.
    """
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Image->PDF: could not delete user's invalid (non-text) filename message: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id,
        "⚠️ Please send the output filename as text (e.g. my_images).",
    )


# --------------------------------------------------------------------------
# Cancel (always confirmed)
# --------------------------------------------------------------------------

_IMG2PDF_STATES = (
    PDFStates.waiting_for_page_size_img2pdf,
    PDFStates.waiting_for_images_to_pdf,
    PDFStates.waiting_for_img2pdf_filename,
)


async def _img2pdf_full_cleanup(state: FSMContext, chat_id: int) -> None:
    cancel_pending_img2pdf_batch(chat_id)
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


@router.callback_query(StateFilter(*_IMG2PDF_STATES), F.data == IMG2PDF_CB_CANCEL)
async def pdf_image_to_pdf_cancel_ask(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _edit_workflow_message(
        query.bot, state,
        "⚠️ Cancel this operation?\n\nYour uploaded images will be discarded.",
        _cancel_confirm_keyboard(),
    )


@router.callback_query(F.data == IMG2PDF_CB_CANCEL_YES)
async def pdf_image_to_pdf_cancel_yes(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    await query.answer()
    await _img2pdf_full_cleanup(state, chat_id)
    try:
        await query.message.delete()
    except Exception as e:
        logger.debug(f"Image->PDF: could not delete workflow message on cancel: {e}")
    await query.bot.send_message(chat_id, "📄 PDF Toolkit -- choose an operation:", reply_markup=get_pdf_menu())


@router.callback_query(F.data == IMG2PDF_CB_CANCEL_NO)
async def pdf_image_to_pdf_cancel_no(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    current_state = await state.get_state()
    if current_state == PDFStates.waiting_for_page_size_img2pdf.state:
        await _edit_workflow_message(query.bot, state, _render_welcome_text(), _page_size_keyboard())
        return
    order = _current_order(data)
    if current_state == PDFStates.waiting_for_img2pdf_filename.state:
        text = (
            f"🖼 Images Added: {len(order)}\n\n"
            "📝 Send the output filename (e.g. my_images) -- I'll add .pdf for you."
        )
        await _edit_workflow_message(query.bot, state, text, _filename_prompt_keyboard())
        return
    if not order:
        page_size = data.get("img2pdf_page_size", PAGE_SIZE_A4)
        await _edit_workflow_message(
            query.bot, state, _render_upload_prompt_text(page_size), _upload_prompt_keyboard()
        )
    else:
        await _show_images_added_screen(query.bot, state, query.message.chat.id, as_new_message=False)


register_stale_callbacks(prefix="img2pdf:")


# --------------------------------------------------------------------------
# PDF -> Images (choose format first, then upload)
# --------------------------------------------------------------------------

P2I_CB_BACK_TO_FORMAT = "pdf_p2i_back_fmt"
P2I_CB_BACK_TO_QUALITY = "pdf_p2i_back_qual"
P2I_CB_BACK_TO_PAGES = "pdf_p2i_back_pages"

P2I_CB_DPI_STD = "pdf_p2i_dpi_std"
P2I_CB_DPI_HIGH = "pdf_p2i_dpi_high"
P2I_CB_DPI_MAX = "pdf_p2i_dpi_max"

P2I_CB_ALL_PAGES = "pdf_p2i_all_pages"
P2I_CB_CUSTOM_PAGES = "pdf_p2i_custom_pages"
P2I_CB_EDIT_PAGES = "pdf_p2i_edit_pages"
P2I_CB_CONVERT = "pdf_p2i_convert"

P2I_CB_CANCEL = "pdf_p2i_cancel"
P2I_CB_CANCEL_YES = "pdf_p2i_cancel_yes"
P2I_CB_CANCEL_NO = "pdf_p2i_cancel_no"

_P2I_DPI_LABELS = {DPI_STANDARD: "Standard", DPI_HIGH: "High", DPI_MAXIMUM: "Maximum"}
_P2I_DPI_BY_CB = {P2I_CB_DPI_STD: DPI_STANDARD, P2I_CB_DPI_HIGH: DPI_HIGH, P2I_CB_DPI_MAX: DPI_MAXIMUM}

_P2I_LARGE_OUTPUT_THRESHOLD = 20
_P2I_ALBUM_SIZE = 10
_P2I_ALBUM_DELAY_SECONDS = 0.8
_P2I_PROGRESS_EDIT_INTERVAL_SECONDS = 2.0

_P2I_CANCELABLE_STATES = (
    PDFStates.waiting_for_format_pdf_to_images,
    PDFStates.waiting_for_quality_pdf_to_images,
    PDFStates.waiting_for_file_pdf_to_images,
    PDFStates.waiting_for_pages_pdf_to_images,
    PDFStates.waiting_for_custom_pages_pdf_to_images,
    PDFStates.waiting_for_ready_pdf_to_images,
)

# A PDF might arrive as part of a Telegram media group -- PDF -> Images only
# ever accepts ONE PDF, so a whole album must be rejected as a unit rather
# than silently taking the first file. Mirrors Split's buffer-then-debounce
# approach (see split.py's `_split_pending_groups`).
_p2i_pending_groups: Dict[str, List[Message]] = {}
_p2i_group_tasks: Dict[str, asyncio.Task] = {}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _p2i_format_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🖼 PNG", callback_data=PDF_TO_IMG_PNG)
    b.button(text="📷 JPG", callback_data=PDF_TO_IMG_JPEG)
    b.button(text="❌ Cancel", callback_data=P2I_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _p2i_quality_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="⭐ Standard · 150 DPI", callback_data=P2I_CB_DPI_STD)
    b.button(text="⭐⭐ High · 300 DPI (Recommended)", callback_data=P2I_CB_DPI_HIGH)
    b.button(text="⭐⭐⭐ Maximum · 600 DPI", callback_data=P2I_CB_DPI_MAX)
    b.button(text="⬅️ Back", callback_data=P2I_CB_BACK_TO_FORMAT)
    b.button(text="❌ Cancel", callback_data=P2I_CB_CANCEL)
    b.adjust(1, 1, 1, 2)
    return b.as_markup()


def _p2i_upload_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=P2I_CB_BACK_TO_QUALITY)
    b.button(text="❌ Cancel", callback_data=P2I_CB_CANCEL)
    b.adjust(2)
    return b.as_markup()


def _p2i_choose_pages_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📄 All Pages", callback_data=P2I_CB_ALL_PAGES)
    b.button(text="✏️ Custom Pages", callback_data=P2I_CB_CUSTOM_PAGES)
    b.button(text="❌ Cancel", callback_data=P2I_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _p2i_custom_pages_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=P2I_CB_BACK_TO_PAGES)
    b.button(text="❌ Cancel", callback_data=P2I_CB_CANCEL)
    b.adjust(2)
    return b.as_markup()


def _p2i_ready_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📸 Convert", callback_data=P2I_CB_CONVERT)
    b.button(text="✏️ Edit Pages", callback_data=P2I_CB_EDIT_PAGES)
    b.button(text="❌ Cancel", callback_data=P2I_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _p2i_cancel_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yes, Cancel", callback_data=P2I_CB_CANCEL_YES)
    b.button(text="↩ Continue", callback_data=P2I_CB_CANCEL_NO)
    b.adjust(2)
    return b.as_markup()


def _p2i_format_label(fmt: str) -> str:
    return "PNG" if fmt == "png" else "JPG"


def _p2i_welcome_text() -> str:
    return (
        "📸 PDF to Images\n\n"
        "Convert PDF pages into high-quality images.\n\n"
        "Choose your preferred output format."
    )


def _p2i_quality_text(fmt: str) -> str:
    return (
        "📸 PDF to Images\n\n"
        f"🖼 Output Format\n\n{_p2i_format_label(fmt)}\n\n"
        "⭐ Image Quality\n\n"
        "Higher quality produces sharper images but increases file size and conversion time.\n\n"
        "Choose the quality you'd like."
    )


def _p2i_upload_text(fmt: str, dpi: int) -> str:
    return (
        "📸 PDF to Images\n\n"
        f"🖼 Output Format\n\n{_p2i_format_label(fmt)}\n\n"
        f"⭐ Image Quality\n\n{dpi} DPI\n\n"
        "Send the PDF you want to convert."
    )


def _p2i_choose_pages_text(total_pages: int) -> str:
    return (
        "📸 PDF Ready\n\n"
        f"📄 Total Pages\n\n{total_pages}\n\n"
        "Choose which pages you'd like to convert."
    )


def _p2i_custom_pages_prompt_text() -> str:
    return (
        "✏️ Custom Pages\n\n"
        "Enter the pages you want to convert.\n\n"
        "Examples\n\n"
        "1\n"
        "1-5\n"
        "1,3,8\n"
        "1-5,8,10-15"
    )


def _p2i_ready_text(data: dict, pages: List[int]) -> str:
    return (
        "📸 PDF Ready\n\n"
        f"📄 Total Pages\n\n{data.get('p2i_page_count', 0)}\n\n"
        f"📑 Selected Pages\n\n{data.get('p2i_pages_label', str(len(pages)))}\n\n"
        f"🖼 Output Format\n\n{_p2i_format_label(data.get('p2i_format', 'png'))}\n\n"
        f"⭐ Image Quality\n\n{data.get('p2i_dpi', DPI_HIGH)} DPI\n\n"
        "Review your settings before converting."
    )


def _p2i_progress_bar(done: int, total: int, width: int = 18) -> str:
    filled = int((done / total) * width) if total else 0
    return "█" * filled + "░" * (width - filled)


def _p2i_sending_progress_text(
    done_albums: int, total_albums: int, est_remaining_seconds: float, pages_range: Optional[str] = None
) -> str:
    pct = int((done_albums / total_albums) * 100) if total_albums else 0
    bar = _p2i_progress_bar(done_albums, total_albums)
    eta = "Almost done..." if est_remaining_seconds < 3 else f"~{int(round(est_remaining_seconds))} seconds"
    lines = [
        "📤 Sending Images...", "",
        bar, "",
        f"Progress\n\n{pct}%", "",
        f"Albums\n\n{done_albums} / {total_albums}",
    ]
    if pages_range:
        lines += ["", f"Pages\n\n{pages_range}"]
    lines += ["", f"ETA\n\n{eta}"]
    return "\n".join(lines)


def _format_p2i_page_ranges(pages: List[int]) -> str:
    """Compact 1-indexed page list -> ranges, e.g. [1,2,3,8] -> '1-3,8'."""
    if not pages:
        return ""
    parts = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        parts.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = p
    parts.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(parts)


def _parse_p2i_pages(spec: str, total_pages: int) -> List[int]:
    """Parse a comma-separated page/range spec (e.g. '1-5,8,10-15') into a
    sorted, deduplicated list of 1-indexed page numbers. Raises ValueError
    with a user-facing message on any problem.
    """
    tokens = [t.strip() for t in spec.split(",") if t.strip() != ""]
    if not tokens:
        raise ValueError("Please enter at least one page.")

    pages: set = set()
    for tok in tokens:
        if "-" in tok:
            bounds = tok.split("-")
            if len(bounds) != 2:
                raise ValueError(
                    "Invalid page selection.\n\nExamples:\n\n1\n1-5\n1,3,7\n1-5,8,10-12"
                )
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                raise ValueError(
                    "Invalid page selection.\n\nExamples:\n\n1\n1-5\n1,3,7\n1-5,8,10-12"
                )
            if start < 1 or end < 1 or start > end:
                raise ValueError(
                    "Invalid page selection.\n\nExamples:\n\n1\n1-5\n1,3,7\n1-5,8,10-12"
                )
            if start > total_pages or end > total_pages:
                raise ValueError(f"Some pages don't exist.\n\nThis PDF contains {total_pages} pages.")
            pages.update(range(start, end + 1))
        else:
            try:
                p = int(tok)
            except ValueError:
                raise ValueError(
                    "Invalid page selection.\n\nExamples:\n\n1\n1-5\n1,3,7\n1-5,8,10-12"
                )
            if p < 1:
                raise ValueError(
                    "Invalid page selection.\n\nExamples:\n\n1\n1-5\n1,3,7\n1-5,8,10-12"
                )
            if p > total_pages:
                raise ValueError(f"Some pages don't exist.\n\nThis PDF contains {total_pages} pages.")
            pages.add(p)

    if not pages:
        raise ValueError("Please enter at least one page.")
    return sorted(pages)


async def _edit_p2i_message(bot, state: FSMContext, text: str, keyboard=None) -> None:
    """Edit the single, reused PDF -> Images workflow message in place.
    This tool keeps exactly one workflow message alive for its whole
    lifetime (per spec) -- everything from the welcome screen through to
    the sending-progress screen edits this same message.
    """
    data = await state.get_data()
    chat_id = data.get("p2i_status_chat_id")
    message_id = data.get("p2i_status_message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
    except Exception as e:
        logger.debug(f"PDF->Images: workflow message edit skipped: {e}")


async def _p2i_full_cleanup(state: FSMContext, chat_id: int) -> None:
    for key in [k for k in _p2i_pending_groups if k.startswith(f"{chat_id}:")]:
        _p2i_pending_groups.pop(key, None)
        task = _p2i_group_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


# --------------------------------------------------------------------------
# Step 1: Welcome / output format
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_PDF_TO_IMAGES)
async def pdf_to_images_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_format_pdf_to_images)
    await query.message.edit_text(_p2i_welcome_text(), reply_markup=_p2i_format_keyboard())
    await state.update_data(
        p2i_status_chat_id=query.message.chat.id,
        p2i_status_message_id=query.message.message_id,
    )
    await query.answer()


@router.callback_query(
    PDFStates.waiting_for_format_pdf_to_images,
    F.data.in_({PDF_TO_IMG_PNG, PDF_TO_IMG_JPEG}),
)
async def pdf_to_images_format_chosen(query: CallbackQuery, state: FSMContext):
    fmt = "png" if query.data == PDF_TO_IMG_PNG else "jpeg"
    await query.answer()
    await state.update_data(p2i_format=fmt)
    await state.set_state(PDFStates.waiting_for_quality_pdf_to_images)
    await _edit_p2i_message(query.bot, state, _p2i_quality_text(fmt), _p2i_quality_keyboard())


@router.callback_query(PDFStates.waiting_for_quality_pdf_to_images, F.data == P2I_CB_BACK_TO_FORMAT)
async def pdf_to_images_back_to_format(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(PDFStates.waiting_for_format_pdf_to_images)
    await _edit_p2i_message(query.bot, state, _p2i_welcome_text(), _p2i_format_keyboard())


# --------------------------------------------------------------------------
# Step 2: Image quality
# --------------------------------------------------------------------------

@router.callback_query(
    PDFStates.waiting_for_quality_pdf_to_images,
    F.data.in_({P2I_CB_DPI_STD, P2I_CB_DPI_HIGH, P2I_CB_DPI_MAX}),
)
async def pdf_to_images_quality_chosen(query: CallbackQuery, state: FSMContext):
    dpi = _P2I_DPI_BY_CB[query.data]
    await query.answer()
    await state.update_data(p2i_dpi=dpi)
    await state.set_state(PDFStates.waiting_for_file_pdf_to_images)
    data = await state.get_data()
    await _edit_p2i_message(query.bot, state, _p2i_upload_text(data.get("p2i_format", "png"), dpi), _p2i_upload_keyboard())


@router.callback_query(PDFStates.waiting_for_file_pdf_to_images, F.data == P2I_CB_BACK_TO_QUALITY)
async def pdf_to_images_back_to_quality(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    await state.set_state(PDFStates.waiting_for_quality_pdf_to_images)
    await _edit_p2i_message(query.bot, state, _p2i_quality_text(data.get("p2i_format", "png")), _p2i_quality_keyboard())


# --------------------------------------------------------------------------
# Step 3/4: Upload PDF, validation, and processing
# --------------------------------------------------------------------------

async def _process_single_p2i_pdf(message: Message, state: FSMContext) -> None:
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF")
    if path is None:
        return  # _download_and_validate already replied with a user-facing error

    filename = message.document.file_name or "document.pdf"

    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"PDF->Images: could not delete upload message: {e}")

    await _edit_p2i_message(
        message.bot, state,
        "⏳ Processing PDF...\n\nPlease wait while your PDF is being analyzed.",
        None,
    )

    try:
        page_count = await PDFToImages().get_page_count(path)
    except PDFProcessingError as e:
        await untrack_temp_files(state, [path])
        delete_paths([path])
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        data = await state.get_data()
        await _edit_p2i_message(
            message.bot, state,
            _p2i_upload_text(data.get("p2i_format", "png"), data.get("p2i_dpi", DPI_HIGH)),
            _p2i_upload_keyboard(),
        )
        return

    await state.update_data(p2i_input_path=path, p2i_filename=filename, p2i_page_count=page_count)
    await state.set_state(PDFStates.waiting_for_pages_pdf_to_images)
    await _edit_p2i_message(message.bot, state, _p2i_choose_pages_text(page_count), _p2i_choose_pages_keyboard())


async def _finalize_p2i_group(key: str, state: FSMContext) -> None:
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    group = _p2i_pending_groups.pop(key, None)
    _p2i_group_tasks.pop(key, None)
    if not group:
        return
    if await state.get_state() != PDFStates.waiting_for_file_pdf_to_images.state:
        return  # user navigated away while the album was still arriving

    if len(group) > 1:
        pdf_count = sum(
            1 for m in group if m.document and validate_extension(m.document.file_name or "", SUPPORTED_PDF_EXTS)
        )
        for m in group:
            try:
                await m.delete()
            except Exception as e:
                logger.debug(f"PDF->Images: could not delete rejected album message: {e}")
        if pdf_count > 1:
            await _send_temp_validation_error(
                group[0].bot, group[0].chat.id,
                "❌ Please send only one PDF.\n\nPDF to Images converts one PDF at a time.",
            )
        else:
            await _send_temp_validation_error(
                group[0].bot, group[0].chat.id, "❌ Please send only one PDF document."
            )
        return

    await _process_single_p2i_pdf(group[0], state)


@router.message(PDFStates.waiting_for_file_pdf_to_images, F.document)
async def pdf_to_images_receive(message: Message, state: FSMContext):
    doc = message.document
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"PDF->Images: could not delete non-PDF upload: {e}")
        await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please send a PDF document.")
        return

    if message.media_group_id:
        key = f"{message.chat.id}:{message.media_group_id}"
        group = _p2i_pending_groups.setdefault(key, [])
        group.append(message)
        old_task = _p2i_group_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        _p2i_group_tasks[key] = asyncio.create_task(_finalize_p2i_group(key, state))
        return

    await _process_single_p2i_pdf(message, state)


@router.message(PDFStates.waiting_for_file_pdf_to_images)
async def pdf_to_images_receive_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"PDF->Images: could not delete invalid upload: {e}")
    await _send_temp_validation_error(message.bot, message.chat.id, "❌ Please send a PDF document.")


_P2I_PDF_LOADED_STATES = (
    PDFStates.waiting_for_pages_pdf_to_images,
    PDFStates.waiting_for_custom_pages_pdf_to_images,
    PDFStates.waiting_for_ready_pdf_to_images,
)


@router.message(StateFilter(*_P2I_PDF_LOADED_STATES), F.document)
async def pdf_to_images_already_loaded(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"PDF->Images: could not delete extra PDF upload: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id,
        "❌ A PDF is already loaded.\n\n"
        "Convert it, edit the page selection, or cancel before uploading another PDF.",
    )


# --------------------------------------------------------------------------
# Step 5: Choose pages (All / Custom)
# --------------------------------------------------------------------------

@router.callback_query(PDFStates.waiting_for_pages_pdf_to_images, F.data == P2I_CB_ALL_PAGES)
async def pdf_to_images_all_pages(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    total = data.get("p2i_page_count", 0)
    pages = list(range(1, total + 1))
    await state.update_data(p2i_pages=pages, p2i_pages_label=str(total), p2i_selection_mode="all")
    await state.set_state(PDFStates.waiting_for_ready_pdf_to_images)
    data = await state.get_data()
    await _edit_p2i_message(query.bot, state, _p2i_ready_text(data, pages), _p2i_ready_keyboard())


@router.callback_query(PDFStates.waiting_for_pages_pdf_to_images, F.data == P2I_CB_CUSTOM_PAGES)
async def pdf_to_images_custom_pages_prompt(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(PDFStates.waiting_for_custom_pages_pdf_to_images)
    await _edit_p2i_message(query.bot, state, _p2i_custom_pages_prompt_text(), _p2i_custom_pages_keyboard())


@router.callback_query(PDFStates.waiting_for_custom_pages_pdf_to_images, F.data == P2I_CB_BACK_TO_PAGES)
async def pdf_to_images_back_to_pages(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    await state.set_state(PDFStates.waiting_for_pages_pdf_to_images)
    await _edit_p2i_message(query.bot, state, _p2i_choose_pages_text(data.get("p2i_page_count", 0)), _p2i_choose_pages_keyboard())


@router.message(PDFStates.waiting_for_custom_pages_pdf_to_images, F.text)
async def pdf_to_images_custom_pages_input(message: Message, state: FSMContext):
    data = await state.get_data()
    total = data.get("p2i_page_count", 0)

    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"PDF->Images: could not delete custom-pages input message: {e}")

    try:
        pages = _parse_p2i_pages(message.text.strip(), total)
    except ValueError as e:
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    label = f"{len(pages)}\n\n({_format_p2i_page_ranges(pages)})"
    await state.update_data(p2i_pages=pages, p2i_pages_label=label, p2i_selection_mode="custom")
    await state.set_state(PDFStates.waiting_for_ready_pdf_to_images)
    data = await state.get_data()
    await _edit_p2i_message(message.bot, state, _p2i_ready_text(data, pages), _p2i_ready_keyboard())


@router.message(PDFStates.waiting_for_custom_pages_pdf_to_images)
async def pdf_to_images_custom_pages_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"PDF->Images: could not delete non-text page input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id,
        "❌ Please send the pages as text.\n\nExamples:\n\n1\n1-5\n1,3,8\n1-5,8,10-15",
    )


# --------------------------------------------------------------------------
# Step 5 (review): PDF Ready -> Convert / Edit Pages
# --------------------------------------------------------------------------

@router.callback_query(PDFStates.waiting_for_ready_pdf_to_images, F.data == P2I_CB_EDIT_PAGES)
async def pdf_to_images_edit_pages(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    if data.get("p2i_selection_mode") == "custom":
        await state.set_state(PDFStates.waiting_for_custom_pages_pdf_to_images)
        await _edit_p2i_message(query.bot, state, _p2i_custom_pages_prompt_text(), _p2i_custom_pages_keyboard())
    else:
        await state.set_state(PDFStates.waiting_for_pages_pdf_to_images)
        await _edit_p2i_message(
            query.bot, state, _p2i_choose_pages_text(data.get("p2i_page_count", 0)), _p2i_choose_pages_keyboard()
        )


# --------------------------------------------------------------------------
# Step 6+: Convert, then send (albums of 10, delayed for large output)
# --------------------------------------------------------------------------

async def _p2i_send_images(
    bot, state: FSMContext, chat_id: int,
    results: List, fmt: str, filename_fn, input_path: str, review_data: dict,
    user_repo, db_user,
) -> None:
    total = len(results)
    chunks = [results[i:i + _P2I_ALBUM_SIZE] for i in range(0, total, _P2I_ALBUM_SIZE)]
    total_albums = len(chunks)
    output_paths = [p for _, p in results]
    cleanup_paths = [input_path] + output_paths
    is_document = fmt == "png"  # PNG -> Documents (lossless); JPG -> Photos (inline preview)

    try:
        if total > _P2I_LARGE_OUTPUT_THRESHOLD:
            await _edit_p2i_message(
                bot, state,
                _p2i_sending_progress_text(0, total_albums, total_albums * _P2I_ALBUM_DELAY_SECONDS),
                None,
            )

        last_edit = 0.0
        for idx, chunk in enumerate(chunks, start=1):
            media = []
            for page_num, path in chunk:
                fname = filename_fn(page_num)
                if is_document:
                    media.append(InputMediaDocument(media=FSInputFile(path, filename=fname)))
                else:
                    media.append(InputMediaPhoto(media=FSInputFile(path, filename=fname)))

            try:
                await bot.send_media_group(chat_id, media)
            except Exception:
                # One retry, matching Split's tolerance for transient
                # Telegram/network hiccups during a multi-message send.
                logger.warning(f"PDF->Images: album {idx}/{total_albums} send failed, retrying once")
                await asyncio.sleep(1.0)
                await bot.send_media_group(chat_id, media)

            if idx < total_albums:
                await asyncio.sleep(_P2I_ALBUM_DELAY_SECONDS)

            if total > _P2I_LARGE_OUTPUT_THRESHOLD:
                now = time.monotonic()
                remaining = (total_albums - idx) * _P2I_ALBUM_DELAY_SECONDS
                if now - last_edit >= _P2I_PROGRESS_EDIT_INTERVAL_SECONDS or idx == total_albums:
                    pages_range = f"{chunk[0][0]}-{chunk[-1][0]}" if len(chunk) > 1 else str(chunk[0][0])
                    await _edit_p2i_message(
                        bot, state,
                        _p2i_sending_progress_text(idx, total_albums, remaining, pages_range=pages_range),
                        None,
                    )
                    last_edit = now
    except Exception:
        logger.exception(f"PDF->Images: failed sending images to chat {chat_id}")
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()
        try:
            await bot.send_message(chat_id, "❌ Failed to convert the PDF.\n\nPlease try again.")
        except Exception:
            pass
        return

    delete_paths(cleanup_paths)
    await untrack_temp_files(state, cleanup_paths)
    await _track_usage(user_repo, db_user)

    status = await state.get_data()
    old_chat_id = status.get("p2i_status_chat_id")
    old_message_id = status.get("p2i_status_message_id")
    await state.clear()
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"PDF->Images: could not delete progress message: {e}")

    await bot.send_message(
        chat_id,
        "✅ Images created successfully!\n\n"
        f"📄 Total Pages\n\n{review_data.get('p2i_page_count', 0)}\n\n"
        f"📑 Converted Pages\n\n{total}\n\n"
        f"🖼 Output Format\n\n{_p2i_format_label(fmt)}\n\n"
        f"⭐ Image Quality\n\n{review_data.get('p2i_dpi', DPI_HIGH)} DPI",
    )
    logger.info(f"PDF->Images: completed for chat {chat_id}, {total} page(s) sent")


@router.callback_query(PDFStates.waiting_for_ready_pdf_to_images, F.data == P2I_CB_CONVERT)
async def pdf_to_images_convert(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    path = data.get("p2i_input_path")
    pages: List[int] = list(data.get("p2i_pages", []))
    if not path or not pages:
        await query.message.answer("Session expired, please start over.")
        await _p2i_full_cleanup(state, chat_id)
        return

    fmt = data.get("p2i_format", "png")
    dpi = data.get("p2i_dpi", DPI_HIGH)

    # From this point Cancel is disabled -- conversion/sending runs to
    # completion (or a hard failure), matching the spec.
    await state.set_state(PDFStates.waiting_for_sending_pdf_to_images)
    await _edit_p2i_message(
        query.bot, state,
        "⏳ Converting PDF...\n\n"
        f"{_p2i_progress_bar(0, 1)}\n\n"
        "Generating images...\n\n"
        "This may take a moment.",
        None,
    )

    try:
        results = await PDFToImages().convert(path, fmt, dpi, pages)
    except Exception as e:
        logger.exception(f"PDF->Images: conversion failed for chat {chat_id}")
        await _edit_p2i_message(query.bot, state, "❌ Failed to convert the PDF.\n\nPlease try again.", None)
        await _p2i_full_cleanup(state, chat_id)
        return

    for _, p in results:
        await track_temp_file(state, p)

    total_pad = max(3, len(str(data.get("p2i_page_count", 0))))
    stem = data.get("p2i_filename", "document.pdf")
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    safe_stem = sanitize_filename(stem) or "document"
    ext = "jpg" if fmt == "jpeg" else "png"

    def filename_fn(page_num: int) -> str:
        return f"{safe_stem} - Page {str(page_num).zfill(total_pad)}.{ext}"

    await _p2i_send_images(query.bot, state, chat_id, results, fmt, filename_fn, path, data, user_repo, db_user)


# --------------------------------------------------------------------------
# Cancel (confirmed; disabled once conversion has started)
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_P2I_CANCELABLE_STATES), F.data == P2I_CB_CANCEL)
async def pdf_to_images_cancel_ask(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(p2i_pre_cancel_state=await state.get_state())
    await _edit_p2i_message(
        query.bot, state,
        "⚠️ Cancel this conversion?\n\nYour uploaded PDF will be discarded.",
        _p2i_cancel_confirm_keyboard(),
    )


@router.callback_query(F.data == P2I_CB_CANCEL_YES)
async def pdf_to_images_cancel_yes(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    await query.answer()
    await _p2i_full_cleanup(state, chat_id)
    try:
        await query.message.delete()
    except Exception as e:
        logger.debug(f"PDF->Images: could not delete workflow message on cancel: {e}")
    await query.bot.send_message(chat_id, "📄 PDF Toolkit -- choose an operation:", reply_markup=get_pdf_menu())


@router.callback_query(F.data == P2I_CB_CANCEL_NO)
async def pdf_to_images_cancel_no(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    prev_state = data.get("p2i_pre_cancel_state")
    await state.set_state(prev_state)

    if prev_state == PDFStates.waiting_for_format_pdf_to_images.state:
        await _edit_p2i_message(query.bot, state, _p2i_welcome_text(), _p2i_format_keyboard())
    elif prev_state == PDFStates.waiting_for_quality_pdf_to_images.state:
        await _edit_p2i_message(query.bot, state, _p2i_quality_text(data.get("p2i_format", "png")), _p2i_quality_keyboard())
    elif prev_state == PDFStates.waiting_for_file_pdf_to_images.state:
        await _edit_p2i_message(
            query.bot, state,
            _p2i_upload_text(data.get("p2i_format", "png"), data.get("p2i_dpi", DPI_HIGH)),
            _p2i_upload_keyboard(),
        )
    elif prev_state == PDFStates.waiting_for_pages_pdf_to_images.state:
        await _edit_p2i_message(query.bot, state, _p2i_choose_pages_text(data.get("p2i_page_count", 0)), _p2i_choose_pages_keyboard())
    elif prev_state == PDFStates.waiting_for_custom_pages_pdf_to_images.state:
        await _edit_p2i_message(query.bot, state, _p2i_custom_pages_prompt_text(), _p2i_custom_pages_keyboard())
    elif prev_state == PDFStates.waiting_for_ready_pdf_to_images.state:
        pages = list(data.get("p2i_pages", []))
        await _edit_p2i_message(query.bot, state, _p2i_ready_text(data, pages), _p2i_ready_keyboard())


register_stale_callbacks(prefix="pdf_p2i_")
