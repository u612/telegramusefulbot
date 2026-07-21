"""Image -> PDF: choose a page size, then upload one or more images (as
Telegram photos, albums, or image documents) and build a single PDF, in
exact upload order. Mirrors Merge's buffer -> debounce -> download-in-
order -> commit architecture (see merge.py) so bursts/albums are handled
identically across the toolkit.

PDF -> Images: choose an output format first, then upload the PDF.
"""
import asyncio
from typing import Dict, List, Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, UnidentifiedImageError

from bot.states.pdf import PDFStates
from bot.keyboards.pdf import (
    PDF_IMAGE_TO_PDF, PDF_PDF_TO_IMAGES, PDF_DONE,
    get_pdf_menu, pdf_to_images_format_keyboard,
    PDF_TO_IMG_PNG, PDF_TO_IMG_JPEG,
)
from bot.keyboards.common import back_home_cancel
from core.constants import SUPPORTED_PDF_EXTS, MERGE_BATCH_FINALIZE_DELAY_SECONDS
from core.logger import logger
from utils.limits import get_effective_limits
from utils.session_manager import register_stale_callbacks

from services.pdf.image_to_pdf import ImageToPDF
from services.pdf.pdf_to_images import PDFToImages
from services.pdf._common import PDFProcessingError

from utils.tempfiles import (
    new_temp_path, track_temp_file, untrack_temp_files,
    get_tracked_files, delete_paths,
)
from utils.validators import validate_extension, validate_file_size, sanitize_filename

from .common import (
    _PDF_MIME, _download_and_validate, _fail, _track_usage,
    _finish_with_documents, _send_temp_validation_error,
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
async def pdf_image_to_pdf_create(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
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
        await state.update_data(img2pdf_creating=True)

        await _edit_workflow_message(
            query.bot, state,
            "⏳ Creating your PDF...\n\nPlease wait while your PDF is being generated.",
            None,
        )

        page_size = data.get("img2pdf_page_size", PAGE_SIZE_A4)
        limits = get_effective_limits(query.from_user.id, db_user)
        try:
            output_path = await ImageToPDF().convert(
                order,
                max_images=limits.image_to_pdf_limit,
                page_size=None if page_size == PAGE_SIZE_ORIGINAL else page_size,
            )
        except Exception as e:
            await _fail(query.message, state, e, order)
            return

        await track_temp_file(state, output_path)
        cleanup_paths = order + [output_path]
        count = len(order)
        # Same naming helper Merge uses to turn a base name into a safe
        # output filename -- Image->PDF has no filename-entry step, so
        # the base name is fixed rather than user-supplied.
        filename = f"{sanitize_filename('images')}.pdf"
        status_data = await state.get_data()
        status_chat_id = status_data.get("img2pdf_status_chat_id")
        status_message_id = status_data.get("img2pdf_status_message_id")
        try:
            await query.bot.send_document(chat_id, FSInputFile(output_path, filename=filename))
            await _track_usage(user_repo, db_user)
            if status_chat_id is not None and status_message_id is not None:
                try:
                    await query.bot.delete_message(chat_id=status_chat_id, message_id=status_message_id)
                except Exception as e:
                    logger.debug(f"Image->PDF: could not delete 'Creating your PDF...' message: {e}")
            await query.bot.send_message(
                chat_id,
                "✅ PDF created successfully!\n\n"
                f"🖼 Images\n\n{count}\n\n"
                f"📄 Page Size\n\n{_PAGE_SIZE_LABELS[page_size]}",
            )
        except PDFProcessingError as e:
            await query.bot.send_message(chat_id, f"⚠️ {e}")
        except Exception:
            logger.exception(f"Image->PDF: failed to send output to user {query.from_user.id}")
            await query.bot.send_message(chat_id, "Something went wrong sending your PDF. Please try again.")
        finally:
            delete_paths(cleanup_paths)
            await untrack_temp_files(state, cleanup_paths)
            await state.clear()
            logger.info(f"Image->PDF: cleanup done for user {query.from_user.id} ({len(cleanup_paths)} temp paths)")


# --------------------------------------------------------------------------
# Cancel (always confirmed)
# --------------------------------------------------------------------------

_IMG2PDF_STATES = (PDFStates.waiting_for_page_size_img2pdf, PDFStates.waiting_for_images_to_pdf)


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


@router.callback_query(F.data == PDF_PDF_TO_IMAGES)
async def pdf_to_images_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_format_pdf_to_images)
    await query.message.edit_text(
        "Choose an output image format:",
        reply_markup=pdf_to_images_format_keyboard(),
    )
    await query.answer()


@router.callback_query(
    PDFStates.waiting_for_format_pdf_to_images,
    F.data.in_({PDF_TO_IMG_PNG, PDF_TO_IMG_JPEG}),
)
async def pdf_to_images_format_chosen(query: CallbackQuery, state: FSMContext):
    fmt = "png" if query.data == PDF_TO_IMG_PNG else "jpeg"
    await state.update_data(pdf_to_images_format=fmt)
    await state.set_state(PDFStates.waiting_for_file_pdf_to_images)
    await query.message.edit_text("Send the PDF file to convert to images.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_pdf_to_images, F.document)
async def pdf_to_images_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return
    data = await state.get_data()
    fmt = data.get("pdf_to_images_format", "png")
    await message.answer("Converting to images... please wait.")
    try:
        outputs = await PDFToImages().convert(path, fmt)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    for out in outputs:
        await track_temp_file(state, out)
    ext = "jpg" if fmt == "jpeg" else "png"
    await _track_usage(user_repo, db_user)
    await _finish_with_documents(
        message, state, outputs,
        filename_fn=lambda i: f"page_{i}.{ext}",
        cleanup_paths=[path] + outputs,
    )
