"""Rearrange Pages: upload -> analyze -> queue one or more page-order
changes (Move Page / Move Range / Swap Pages / Reverse Order) -> review
Pending Changes -> Ready screen -> process/upload. Mirrors Merge / Split /
Compress / Rotate / Extract's UX philosophy exactly: clean chat, minimal
messages, Telegram-native, context-aware Cancel, /start resets everything
(via the generic tracked-files + state.clear() path in
bot.handlers.base._reset_to_main_menu).

Unlike the other single-shot tools, Rearrange lets the user queue several
operations before applying anything. Operations are executed strictly in
the order they were queued, against the arrangement as it stands at that
point in the sequence -- nothing is merged, optimized, or reordered.
"""
import asyncio
from typing import Dict, List, Optional, Tuple

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.pdf import get_pdf_menu, PDF_REARRANGE
from core.constants import SUPPORTED_PDF_EXTS, MERGE_BATCH_FINALIZE_DELAY_SECONDS
from core.logger import logger

from pypdf import PdfWriter

from services.pdf._common import PDFProcessingError, open_pdf_reader, check_page_count

from utils.tempfiles import track_temp_file, untrack_temp_files, delete_paths, get_tracked_files, new_temp_path
from utils.validators import validate_extension

from .common import (
    _PDF_MIME,
    _QUEUE_DIVIDER,
    _download_and_validate,
    _fail,
    _track_usage,
    _format_size,
    _display_name,
    _send_temp_validation_error,
)

router = Router()

# --------------------------------------------------------------------------
# Rearrange Pages (own StatesGroup so this file is fully self-contained and
# nothing outside Rearrange needs to change; the generic /start reset in
# bot.handlers.base works on tracked temp files + state.clear() regardless
# of which StatesGroup a state belongs to.)
# --------------------------------------------------------------------------


class RearrangeStates(StatesGroup):
    waiting_for_file_rearrange = State()
    waiting_for_one_page_error = State()
    waiting_for_main_menu = State()
    waiting_for_move_page_number = State()
    waiting_for_move_range_input = State()
    waiting_for_swap_input = State()
    waiting_for_destination_choice = State()
    waiting_for_destination_number = State()
    waiting_for_change_added = State()
    waiting_for_pending_changes = State()
    waiting_for_remove_change = State()
    waiting_for_clear_all_confirm = State()
    waiting_for_ready = State()


REARR_CB_CANCEL = "pdfrearr:cancel"

REARR_CB_UPLOAD_ANOTHER = "pdfrearr:upload_another"

REARR_CB_MOVE_PAGE = "pdfrearr:move_page"
REARR_CB_MOVE_RANGE = "pdfrearr:move_range"
REARR_CB_SWAP = "pdfrearr:swap"
REARR_CB_REVERSE = "pdfrearr:reverse"
REARR_CB_PENDING = "pdfrearr:pending"

REARR_CB_DEST_BEFORE = "pdfrearr:dest_before"
REARR_CB_DEST_AFTER = "pdfrearr:dest_after"
REARR_CB_DEST_START = "pdfrearr:dest_start"
REARR_CB_DEST_END = "pdfrearr:dest_end"

REARR_CB_ADD_CHANGE = "pdfrearr:add_change"
REARR_CB_OPEN_READY = "pdfrearr:open_ready"
REARR_CB_APPLY = "pdfrearr:apply"

REARR_CB_REMOVE_CHANGE_MENU = "pdfrearr:remove_menu"
REARR_CB_REMOVE_ITEM_PREFIX = "pdfrearr:rm:"
REARR_CB_CLEAR_ALL = "pdfrearr:clear_all"
REARR_CB_CLEAR_ALL_YES = "pdfrearr:clear_all_yes"

REARR_CB_BACK = "pdfrearr:back"

_REARRANGE_STATES = (
    RearrangeStates.waiting_for_file_rearrange,
    RearrangeStates.waiting_for_one_page_error,
    RearrangeStates.waiting_for_main_menu,
    RearrangeStates.waiting_for_move_page_number,
    RearrangeStates.waiting_for_move_range_input,
    RearrangeStates.waiting_for_swap_input,
    RearrangeStates.waiting_for_destination_choice,
    RearrangeStates.waiting_for_destination_number,
    RearrangeStates.waiting_for_change_added,
    RearrangeStates.waiting_for_pending_changes,
    RearrangeStates.waiting_for_remove_change,
    RearrangeStates.waiting_for_clear_all_confirm,
    RearrangeStates.waiting_for_ready,
)

# Buffers for the rare case a PDF arrives as part of a Telegram media group
# (album) -- Rearrange only ever accepts ONE PDF, so a whole album must be
# rejected as a unit. Mirrors Rotate/Extract's buffer-then-debounce approach.
_rearr_pending_groups: Dict[str, List[Message]] = {}
_rearr_group_tasks: Dict[str, "asyncio.Task"] = {}

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _circled(n: int) -> str:
    if 1 <= n <= len(_CIRCLED):
        return _CIRCLED[n - 1]
    return f"({n})"


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------

def _upload_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data=REARR_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _one_page_error_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📄 Upload Another PDF", callback_data=REARR_CB_UPLOAD_ANOTHER)
    b.button(text="❌ Cancel", callback_data=REARR_CB_CANCEL)
    b.adjust(1, 1)
    return b.as_markup()


def _main_menu_keyboard(queue_len: int):
    b = InlineKeyboardBuilder()
    b.button(text="📄 Move Page", callback_data=REARR_CB_MOVE_PAGE)
    b.button(text="📑 Move Range", callback_data=REARR_CB_MOVE_RANGE)
    b.button(text="🔄 Swap Pages", callback_data=REARR_CB_SWAP)
    b.button(text="↕️ Reverse Order", callback_data=REARR_CB_REVERSE)
    rows = [2, 2]
    if queue_len > 0:
        b.button(text=f"📝 Pending Changes ({queue_len})", callback_data=REARR_CB_PENDING)
        rows.append(1)
    b.button(text="❌ Cancel", callback_data=REARR_CB_CANCEL)
    rows.append(1)
    b.adjust(*rows)
    return b.as_markup()


def _back_cancel_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Back", callback_data=REARR_CB_BACK)
    b.button(text="❌ Cancel", callback_data=REARR_CB_CANCEL)
    b.adjust(1, 1)
    return b.as_markup()


def _destination_choice_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="⬆️ Before Page", callback_data=REARR_CB_DEST_BEFORE)
    b.button(text="⬇️ After Page", callback_data=REARR_CB_DEST_AFTER)
    b.button(text="🏠 Beginning", callback_data=REARR_CB_DEST_START)
    b.button(text="🏁 End", callback_data=REARR_CB_DEST_END)
    b.button(text="🔙 Back", callback_data=REARR_CB_BACK)
    b.button(text="❌ Cancel", callback_data=REARR_CB_CANCEL)
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def _change_added_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Add Change", callback_data=REARR_CB_ADD_CHANGE)
    b.button(text="📝 Pending Changes", callback_data=REARR_CB_PENDING)
    b.button(text="✅ Rearrange", callback_data=REARR_CB_OPEN_READY)
    b.button(text="❌ Cancel", callback_data=REARR_CB_CANCEL)
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def _pending_changes_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Add Change", callback_data=REARR_CB_ADD_CHANGE)
    b.button(text="🗑 Remove Change", callback_data=REARR_CB_REMOVE_CHANGE_MENU)
    b.button(text="♻️ Clear All", callback_data=REARR_CB_CLEAR_ALL)
    b.button(text="✅ Rearrange", callback_data=REARR_CB_OPEN_READY)
    b.button(text="🔙 Back", callback_data=REARR_CB_BACK)
    b.adjust(1, 1, 1, 1, 1)
    return b.as_markup()


def _remove_change_keyboard(queue: List[dict]):
    b = InlineKeyboardBuilder()
    for i, op in enumerate(queue):
        b.button(text=f"{_circled(i + 1)} {_op_button_label(op)}", callback_data=f"{REARR_CB_REMOVE_ITEM_PREFIX}{i}")
    b.button(text="🔙 Back", callback_data=REARR_CB_BACK)
    b.adjust(1)
    return b.as_markup()


def _clear_all_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Yes", callback_data=REARR_CB_CLEAR_ALL_YES)
    b.button(text="🔙 Back", callback_data=REARR_CB_BACK)
    b.adjust(1, 1)
    return b.as_markup()


def _ready_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Rearrange", callback_data=REARR_CB_APPLY)
    b.button(text="🔙 Back", callback_data=REARR_CB_BACK)
    b.button(text="❌ Cancel", callback_data=REARR_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


# --------------------------------------------------------------------------
# Operation formatting
# --------------------------------------------------------------------------

def _dest_desc(op: dict) -> str:
    d = op["dest"]
    if d == "before":
        return f"Before page {op['dest_page']}"
    if d == "after":
        return f"After page {op['dest_page']}"
    if d == "start":
        return "Beginning"
    return "End"


def _op_full_desc(op: dict) -> str:
    t = op["type"]
    if t == "move_page":
        return f"Move page {op['page']}\n→ {_dest_desc(op)}"
    if t == "move_range":
        return f"Move pages {op['start']}–{op['end']}\n→ {_dest_desc(op)}"
    if t == "swap":
        return f"Swap page {op['a']}\n↔ Page {op['b']}"
    return "Reverse page order"


def _op_button_label(op: dict) -> str:
    t = op["type"]
    if t == "move_page":
        return f"Move page {op['page']}"
    if t == "move_range":
        return f"Move pages {op['start']}–{op['end']}"
    if t == "swap":
        return "Swap pages"
    return "Reverse order"


def _render_queue_block(queue: List[dict]) -> str:
    lines = []
    for i, op in enumerate(queue):
        lines.append(f"{_circled(i + 1)} {_op_full_desc(op)}")
    return "\n\n".join(lines)


# --------------------------------------------------------------------------
# Screen text renderers
# --------------------------------------------------------------------------

def _render_upload_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "🔀 Rearrange Pages\n\n"
        "Send one PDF.\n\n"
        "Make one or more page order changes before applying them.\n\n"
        "Supported\n"
        "• One PDF only\n\n"
        "Type /start anytime to return home.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_one_page_error_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "❌ Cannot Rearrange PDF\n\n"
        "This PDF contains only one page.\n\n"
        "Rearranging requires at least two pages.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_main_menu_text(filename: str, page_count: int, size_bytes: Optional[int], queue: List[dict]) -> str:
    if queue:
        return (
            f"{_QUEUE_DIVIDER}\n"
            "✅ PDF Loaded\n\n"
            f"📄 {_display_name(filename)}\n\n"
            f"{page_count} Pages • {_format_size(size_bytes)}\n\n"
            f"Pending Changes\n{len(queue)}\n\n"
            "Choose another change or review your changes.\n"
            f"{_QUEUE_DIVIDER}"
        )
    return (
        f"{_QUEUE_DIVIDER}\n"
        "✅ PDF Loaded\n\n"
        f"📄 {_display_name(filename)}\n\n"
        f"{page_count} Pages • {_format_size(size_bytes)}\n\n"
        "Choose your first change.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_move_page_number_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Move Page\n\n"
        "Enter the page number.\n\n"
        "Example\n"
        "8\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_move_range_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Move Range\n\n"
        "Enter page range.\n\n"
        "Examples\n"
        "5-12\n"
        "30-40\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_swap_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Swap Pages\n\n"
        "Enter two page numbers.\n\n"
        "Example\n"
        "8,25\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_destination_choice_text(kind: str, data: dict) -> str:
    if kind == "move_page":
        page = data.get("rearr_pending_page")
        return f"{_QUEUE_DIVIDER}\nWhere should page {page} go?\n{_QUEUE_DIVIDER}"
    start, end = data.get("rearr_pending_range")
    return f"{_QUEUE_DIVIDER}\nWhere should pages {start}–{end} go?\n{_QUEUE_DIVIDER}"


def _render_destination_number_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Enter destination page.\n\n"
        "Example\n"
        "5\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_change_added_text(queue: List[dict]) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "✅ Change Added\n\n"
        "Current Queue\n\n"
        f"{_render_queue_block(queue)}\n\n"
        f"Pending Changes\n{len(queue)}\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_pending_changes_text(queue: List[dict]) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Pending Changes\n\n"
        f"{_render_queue_block(queue)}\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_remove_change_text() -> str:
    return f"{_QUEUE_DIVIDER}\nSelect a change to remove.\n{_QUEUE_DIVIDER}"


def _render_clear_all_text() -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Clear all pending changes?\n\n"
        "This action cannot be undone.\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_ready_text(filename: str, queue: List[dict]) -> str:
    lines = []
    for op in queue:
        t = op["type"]
        if t == "move_page":
            lines.append(f"• Move page {op['page']} {_dest_desc(op).lower()}")
        elif t == "move_range":
            lines.append(f"• Move pages {op['start']}–{op['end']} {_dest_desc(op).lower()}")
        elif t == "swap":
            lines.append(f"• Swap page {op['a']} with page {op['b']}")
        else:
            lines.append("• Reverse page order")
    body = "\n\n".join(lines)
    return (
        f"{_QUEUE_DIVIDER}\n"
        "Ready to Rearrange\n\n"
        f"📄 {_display_name(filename)}\n\n"
        "Pending Changes\n\n"
        f"{body}\n"
        f"{_QUEUE_DIVIDER}"
    )


def _render_completion_caption(filename: str, applied_count: int) -> str:
    return (
        f"{_QUEUE_DIVIDER}\n"
        "✅ Rearrangement Complete\n\n"
        f"Output\n{_display_name(filename)}\n\n"
        f"Applied\n{applied_count} changes\n"
        f"{_QUEUE_DIVIDER}"
    )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _parse_page_number(text: str, total_pages: int) -> int:
    try:
        p = int(text.strip())
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid page number.\n\nEnter a page between 1 and {total_pages}.")
    if p < 1 or p > total_pages:
        raise ValueError(f"Invalid page number.\n\nEnter a page between 1 and {total_pages}.")
    return p


def _parse_range(text: str, total_pages: int) -> Tuple[int, int]:
    parts = text.strip().split("-")
    if len(parts) != 2:
        raise ValueError("Invalid page range.\n\nExample\n5-12")
    try:
        start, end = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        raise ValueError("Invalid page range.\n\nExample\n5-12")
    if start < 1 or end > total_pages or start > end:
        raise ValueError("Invalid page range.\n\nExample\n5-12")
    return start, end


def _parse_swap(text: str, total_pages: int) -> Tuple[int, int]:
    parts = text.strip().split(",")
    if len(parts) != 2:
        raise ValueError("Invalid page numbers.\n\nExample\n8,25")
    try:
        a, b = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        raise ValueError("Invalid page numbers.\n\nExample\n8,25")
    if a < 1 or a > total_pages or b < 1 or b > total_pages:
        raise ValueError(f"Invalid page number.\n\nEnter pages between 1 and {total_pages}.")
    if a == b:
        raise ValueError("Pages must be different.")
    return a, b


def _validate_destination_page_for_move_page(dest_page: int, source_page: int) -> None:
    if dest_page == source_page:
        raise ValueError("Invalid destination page.\n\nDestination cannot equal source.")


def _validate_destination_page_for_move_range(dest_page: int, start: int, end: int) -> None:
    if start <= dest_page <= end:
        raise ValueError("Invalid destination page.\n\nDestination cannot be inside the moved range.")


# --------------------------------------------------------------------------
# Applying the queued operations
# --------------------------------------------------------------------------

def _move_slice(order: List[int], s_idx: int, e_idx: int, dest: str, dest_page: Optional[int]) -> List[int]:
    items = order[s_idx:e_idx + 1]
    del order[s_idx:e_idx + 1]
    n = len(items)
    if dest == "start":
        insert_at = 0
    elif dest == "end":
        insert_at = len(order)
    else:
        d_idx = dest_page - 1
        if d_idx > e_idx:
            d_idx -= n
        elif d_idx >= s_idx:
            d_idx = s_idx
        insert_at = d_idx if dest == "before" else d_idx + 1
        insert_at = max(0, min(insert_at, len(order)))
    order[insert_at:insert_at] = items
    return order


def _apply_operations(total_pages: int, queue: List[dict]) -> List[int]:
    order = list(range(total_pages))
    for op in queue:
        t = op["type"]
        if t == "reverse":
            order.reverse()
        elif t == "swap":
            ia, ib = op["a"] - 1, op["b"] - 1
            order[ia], order[ib] = order[ib], order[ia]
        elif t == "move_page":
            order = _move_slice(order, op["page"] - 1, op["page"] - 1, op["dest"], op.get("dest_page"))
        elif t == "move_range":
            order = _move_slice(order, op["start"] - 1, op["end"] - 1, op["dest"], op.get("dest_page"))
    return order


def _rearrange_sync(input_path: str, queue: List[dict]) -> str:
    reader = open_pdf_reader(input_path)
    total_pages = check_page_count(reader, min_pages=2)
    order = _apply_operations(total_pages, queue)

    writer = PdfWriter()
    try:
        for idx in order:
            writer.add_page(reader.pages[idx])

        output_path = new_temp_path(suffix=".pdf")
        with open(output_path, "wb") as f:
            writer.write(f)
        logger.info(f"Rearranged {input_path} with {len(queue)} queued change(s) -> {output_path}")
        return output_path
    finally:
        writer.close()


async def _rearrange_apply(input_path: str, queue: List[dict]) -> str:
    return await asyncio.to_thread(_rearrange_sync, input_path, queue)


# --------------------------------------------------------------------------
# Message replace + cleanup helpers
# --------------------------------------------------------------------------

async def _replace_rearr_message(bot, state: FSMContext, chat_id: int, text: str, keyboard=None) -> None:
    """Delete the previous Rearrange status message (if any) and send a
    fresh one -- same 'never edit, always replace' pattern Rotate/Extract
    use, so the newest Rearrange screen always sits at the bottom of chat.
    """
    data = await state.get_data()
    old_chat_id = data.get("rearr_status_chat_id")
    old_message_id = data.get("rearr_status_message_id")
    if old_chat_id is not None and old_message_id is not None:
        try:
            await bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Rearrange: status delete skipped: {e}")

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await state.update_data(rearr_status_chat_id=chat_id, rearr_status_message_id=sent.message_id)


async def _rearr_full_cleanup(state: FSMContext, chat_id: int) -> None:
    for key in [k for k in _rearr_pending_groups if k.startswith(f"{chat_id}:")]:
        _rearr_pending_groups.pop(key, None)
        task = _rearr_group_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


async def _reject_rearr_upload(message: Message, reason: str) -> None:
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rearrange: could not delete invalid upload message: {e}")
    await _send_temp_validation_error(message.bot, message.chat.id, reason)


async def _show_main_menu(bot, state: FSMContext, chat_id: int) -> None:
    data = await state.get_data()
    queue = data.get("rearr_queue", [])
    await state.set_state(RearrangeStates.waiting_for_main_menu)
    await _replace_rearr_message(
        bot, state, chat_id,
        _render_main_menu_text(
            data.get("rearr_filename", "document.pdf"),
            data.get("rearr_page_count", 0),
            data.get("rearr_file_size"),
            queue,
        ),
        _main_menu_keyboard(len(queue)),
    )


async def _show_pending_changes(bot, state: FSMContext, chat_id: int) -> None:
    data = await state.get_data()
    queue = data.get("rearr_queue", [])
    await state.set_state(RearrangeStates.waiting_for_pending_changes)
    await _replace_rearr_message(
        bot, state, chat_id,
        _render_pending_changes_text(queue),
        _pending_changes_keyboard(),
    )


# --------------------------------------------------------------------------
# Step 1: entry + waiting for PDF
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_REARRANGE)
async def pdf_rearrange_start(query: CallbackQuery, state: FSMContext):
    chat_id = query.message.chat.id
    await state.set_state(RearrangeStates.waiting_for_file_rearrange)
    await query.message.edit_text(_render_upload_text(), reply_markup=_upload_keyboard())
    await state.update_data(
        rearr_status_chat_id=chat_id,
        rearr_status_message_id=query.message.message_id,
    )
    await query.answer()


async def _process_single_rearr_pdf(message: Message, state: FSMContext, db_user=None) -> None:
    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        return

    try:
        reader = open_pdf_reader(path)
        page_count = check_page_count(reader, min_pages=1)
    except PDFProcessingError as e:
        await untrack_temp_files(state, [path])
        delete_paths([path])
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    filename = message.document.file_name or "document.pdf"
    size_bytes = message.document.file_size

    await state.update_data(
        rearr_input_path=path,
        rearr_filename=filename,
        rearr_page_count=page_count,
        rearr_file_size=size_bytes,
        rearr_queue=[],
    )

    if page_count == 1:
        await state.set_state(RearrangeStates.waiting_for_one_page_error)
        await _replace_rearr_message(
            message.bot, state, message.chat.id,
            _render_one_page_error_text(), _one_page_error_keyboard(),
        )
        return

    await _show_main_menu(message.bot, state, message.chat.id)


async def _finalize_rearr_media_group(key: str, state: FSMContext, db_user) -> None:
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    group = _rearr_pending_groups.pop(key, None)
    _rearr_group_tasks.pop(key, None)
    if not group:
        return
    if await state.get_state() != RearrangeStates.waiting_for_file_rearrange.state:
        return  # user navigated away while the album was still arriving

    if len(group) > 1:
        for m in group:
            try:
                await m.delete()
            except Exception as e:
                logger.debug(f"Rearrange: could not delete rejected album message: {e}")
        await _send_temp_validation_error(
            group[0].bot, group[0].chat.id,
            "❌ Please send only ONE PDF file.\n\nRearrange Pages works with one document at a time.",
        )
        return

    await _process_single_rearr_pdf(group[0], state, db_user=db_user)


@router.message(RearrangeStates.waiting_for_file_rearrange, F.document)
async def pdf_rearrange_receive(message: Message, state: FSMContext, db_user=None):
    doc = message.document
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await _reject_rearr_upload(message, "❌ Please send a PDF file only.")
        return

    if message.media_group_id:
        key = f"{message.chat.id}:{message.media_group_id}"
        group = _rearr_pending_groups.setdefault(key, [])
        group.append(message)
        old_task = _rearr_group_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        _rearr_group_tasks[key] = asyncio.create_task(
            _finalize_rearr_media_group(key, state, db_user)
        )
        return

    await _process_single_rearr_pdf(message, state, db_user=db_user)


@router.message(RearrangeStates.waiting_for_file_rearrange)
async def pdf_rearrange_receive_invalid(message: Message):
    """Any non-PDF content is rejected the same way -- delete it, show a
    temporary error, stay put.
    """
    await _reject_rearr_upload(message, "❌ Please send a PDF file only.")


# --------------------------------------------------------------------------
# One-page error screen
# --------------------------------------------------------------------------

@router.callback_query(RearrangeStates.waiting_for_one_page_error, F.data == REARR_CB_UPLOAD_ANOTHER)
async def pdf_rearrange_upload_another(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    old_path = data.get("rearr_input_path")
    if old_path:
        await untrack_temp_files(state, [old_path])
        delete_paths([old_path])
    await state.update_data(
        rearr_input_path=None, rearr_filename=None, rearr_page_count=None,
        rearr_file_size=None, rearr_queue=[],
    )
    await state.set_state(RearrangeStates.waiting_for_file_rearrange)
    await _replace_rearr_message(query.bot, state, chat_id, _render_upload_text(), _upload_keyboard())


# --------------------------------------------------------------------------
# Main Menu -- entry points into each operation
# --------------------------------------------------------------------------

@router.callback_query(RearrangeStates.waiting_for_main_menu, F.data == REARR_CB_MOVE_PAGE)
async def pdf_rearrange_move_page_entry(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(RearrangeStates.waiting_for_move_page_number)
    await _replace_rearr_message(
        query.bot, state, query.message.chat.id,
        _render_move_page_number_text(), _back_cancel_keyboard(),
    )


@router.callback_query(RearrangeStates.waiting_for_main_menu, F.data == REARR_CB_MOVE_RANGE)
async def pdf_rearrange_move_range_entry(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(RearrangeStates.waiting_for_move_range_input)
    await _replace_rearr_message(
        query.bot, state, query.message.chat.id,
        _render_move_range_text(), _back_cancel_keyboard(),
    )


@router.callback_query(RearrangeStates.waiting_for_main_menu, F.data == REARR_CB_SWAP)
async def pdf_rearrange_swap_entry(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(RearrangeStates.waiting_for_swap_input)
    await _replace_rearr_message(
        query.bot, state, query.message.chat.id,
        _render_swap_text(), _back_cancel_keyboard(),
    )


@router.callback_query(RearrangeStates.waiting_for_main_menu, F.data == REARR_CB_REVERSE)
async def pdf_rearrange_reverse(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    queue = list(data.get("rearr_queue", []))
    queue.append({"type": "reverse"})
    await state.update_data(rearr_queue=queue)
    await state.set_state(RearrangeStates.waiting_for_change_added)
    await _replace_rearr_message(
        query.bot, state, query.message.chat.id,
        _render_change_added_text(queue), _change_added_keyboard(),
    )


@router.callback_query(RearrangeStates.waiting_for_main_menu, F.data == REARR_CB_PENDING)
async def pdf_rearrange_open_pending_from_menu(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _show_pending_changes(query.bot, state, query.message.chat.id)


# --------------------------------------------------------------------------
# Move Page: number input -> destination choice -> [destination number] -> queue
# --------------------------------------------------------------------------

@router.message(RearrangeStates.waiting_for_move_page_number, F.text)
async def pdf_rearrange_move_page_number(message: Message, state: FSMContext):
    data = await state.get_data()
    total_pages = data.get("rearr_page_count", 0)
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rearrange: could not delete move-page input message: {e}")

    try:
        page = _parse_page_number(message.text, total_pages)
    except ValueError as e:
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    await state.update_data(rearr_pending_kind="move_page", rearr_pending_page=page)
    await state.set_state(RearrangeStates.waiting_for_destination_choice)
    fresh_data = await state.get_data()
    await _replace_rearr_message(
        message.bot, state, message.chat.id,
        _render_destination_choice_text("move_page", fresh_data), _destination_choice_keyboard(),
    )


@router.message(RearrangeStates.waiting_for_move_page_number)
async def pdf_rearrange_move_page_number_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rearrange: could not delete non-text move-page input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id, "❌ Invalid page number.\n\nEnter a page number, e.g. 8",
    )


# --------------------------------------------------------------------------
# Move Range: range input -> destination choice -> [destination number] -> queue
# --------------------------------------------------------------------------

@router.message(RearrangeStates.waiting_for_move_range_input, F.text)
async def pdf_rearrange_move_range_input(message: Message, state: FSMContext):
    data = await state.get_data()
    total_pages = data.get("rearr_page_count", 0)
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rearrange: could not delete move-range input message: {e}")

    try:
        start, end = _parse_range(message.text, total_pages)
    except ValueError as e:
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    await state.update_data(rearr_pending_kind="move_range", rearr_pending_range=(start, end))
    await state.set_state(RearrangeStates.waiting_for_destination_choice)
    fresh_data = await state.get_data()
    await _replace_rearr_message(
        message.bot, state, message.chat.id,
        _render_destination_choice_text("move_range", fresh_data), _destination_choice_keyboard(),
    )


@router.message(RearrangeStates.waiting_for_move_range_input)
async def pdf_rearrange_move_range_input_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rearrange: could not delete non-text move-range input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id, "❌ Invalid page range.\n\nExample\n5-12",
    )


# --------------------------------------------------------------------------
# Swap Pages: two-number input -> queue directly
# --------------------------------------------------------------------------

@router.message(RearrangeStates.waiting_for_swap_input, F.text)
async def pdf_rearrange_swap_input(message: Message, state: FSMContext):
    data = await state.get_data()
    total_pages = data.get("rearr_page_count", 0)
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rearrange: could not delete swap input message: {e}")

    try:
        a, b = _parse_swap(message.text, total_pages)
    except ValueError as e:
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    queue = list(data.get("rearr_queue", []))
    queue.append({"type": "swap", "a": a, "b": b})
    await state.update_data(rearr_queue=queue)
    await state.set_state(RearrangeStates.waiting_for_change_added)
    await _replace_rearr_message(
        message.bot, state, message.chat.id,
        _render_change_added_text(queue), _change_added_keyboard(),
    )


@router.message(RearrangeStates.waiting_for_swap_input)
async def pdf_rearrange_swap_input_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rearrange: could not delete non-text swap input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id, "❌ Invalid page numbers.\n\nExample\n8,25",
    )


# --------------------------------------------------------------------------
# Shared destination-choice screen (Move Page & Move Range)
# --------------------------------------------------------------------------

async def _queue_move_op_and_show_change_added(bot, state: FSMContext, chat_id: int) -> None:
    data = await state.get_data()
    kind = data.get("rearr_pending_kind")
    queue = list(data.get("rearr_queue", []))
    if kind == "move_page":
        queue.append({
            "type": "move_page",
            "page": data.get("rearr_pending_page"),
            "dest": data.get("rearr_pending_dest"),
            "dest_page": data.get("rearr_pending_dest_page"),
        })
    else:
        start, end = data.get("rearr_pending_range")
        queue.append({
            "type": "move_range",
            "start": start,
            "end": end,
            "dest": data.get("rearr_pending_dest"),
            "dest_page": data.get("rearr_pending_dest_page"),
        })
    await state.update_data(
        rearr_queue=queue,
        rearr_pending_kind=None,
        rearr_pending_page=None,
        rearr_pending_range=None,
        rearr_pending_dest=None,
        rearr_pending_dest_page=None,
    )
    await state.set_state(RearrangeStates.waiting_for_change_added)
    await _replace_rearr_message(
        bot, state, chat_id,
        _render_change_added_text(queue), _change_added_keyboard(),
    )


@router.callback_query(RearrangeStates.waiting_for_destination_choice, F.data == REARR_CB_DEST_START)
async def pdf_rearrange_dest_start(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(rearr_pending_dest="start", rearr_pending_dest_page=None)
    await _queue_move_op_and_show_change_added(query.bot, state, query.message.chat.id)


@router.callback_query(RearrangeStates.waiting_for_destination_choice, F.data == REARR_CB_DEST_END)
async def pdf_rearrange_dest_end(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(rearr_pending_dest="end", rearr_pending_dest_page=None)
    await _queue_move_op_and_show_change_added(query.bot, state, query.message.chat.id)


@router.callback_query(RearrangeStates.waiting_for_destination_choice, F.data == REARR_CB_DEST_BEFORE)
async def pdf_rearrange_dest_before(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(rearr_pending_dest="before")
    await state.set_state(RearrangeStates.waiting_for_destination_number)
    await _replace_rearr_message(
        query.bot, state, query.message.chat.id,
        _render_destination_number_text(), _back_cancel_keyboard(),
    )


@router.callback_query(RearrangeStates.waiting_for_destination_choice, F.data == REARR_CB_DEST_AFTER)
async def pdf_rearrange_dest_after(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(rearr_pending_dest="after")
    await state.set_state(RearrangeStates.waiting_for_destination_number)
    await _replace_rearr_message(
        query.bot, state, query.message.chat.id,
        _render_destination_number_text(), _back_cancel_keyboard(),
    )


@router.message(RearrangeStates.waiting_for_destination_number, F.text)
async def pdf_rearrange_dest_number(message: Message, state: FSMContext):
    data = await state.get_data()
    total_pages = data.get("rearr_page_count", 0)
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rearrange: could not delete destination-number input: {e}")

    try:
        dest_page = _parse_page_number(message.text, total_pages)
        kind = data.get("rearr_pending_kind")
        if kind == "move_page":
            _validate_destination_page_for_move_page(dest_page, data.get("rearr_pending_page"))
        else:
            start, end = data.get("rearr_pending_range")
            _validate_destination_page_for_move_range(dest_page, start, end)
    except ValueError as e:
        await _send_temp_validation_error(message.bot, message.chat.id, f"❌ {e}")
        return

    await state.update_data(rearr_pending_dest_page=dest_page)
    await _queue_move_op_and_show_change_added(message.bot, state, message.chat.id)


@router.message(RearrangeStates.waiting_for_destination_number)
async def pdf_rearrange_dest_number_invalid(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Rearrange: could not delete non-text destination-number input: {e}")
    await _send_temp_validation_error(
        message.bot, message.chat.id, "❌ Invalid destination page.",
    )


# --------------------------------------------------------------------------
# Change Added screen
# --------------------------------------------------------------------------

@router.callback_query(RearrangeStates.waiting_for_change_added, F.data == REARR_CB_ADD_CHANGE)
async def pdf_rearrange_add_change(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _show_main_menu(query.bot, state, query.message.chat.id)


@router.callback_query(RearrangeStates.waiting_for_change_added, F.data == REARR_CB_PENDING)
async def pdf_rearrange_open_pending_from_change_added(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _show_pending_changes(query.bot, state, query.message.chat.id)


# --------------------------------------------------------------------------
# Pending Changes screen
# --------------------------------------------------------------------------

@router.callback_query(RearrangeStates.waiting_for_pending_changes, F.data == REARR_CB_ADD_CHANGE)
async def pdf_rearrange_pending_add_change(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _show_main_menu(query.bot, state, query.message.chat.id)


@router.callback_query(RearrangeStates.waiting_for_pending_changes, F.data == REARR_CB_REMOVE_CHANGE_MENU)
async def pdf_rearrange_open_remove_change(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    queue = data.get("rearr_queue", [])
    await state.set_state(RearrangeStates.waiting_for_remove_change)
    await _replace_rearr_message(
        query.bot, state, query.message.chat.id,
        _render_remove_change_text(), _remove_change_keyboard(queue),
    )


@router.callback_query(RearrangeStates.waiting_for_pending_changes, F.data == REARR_CB_CLEAR_ALL)
async def pdf_rearrange_open_clear_all(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(RearrangeStates.waiting_for_clear_all_confirm)
    await _replace_rearr_message(
        query.bot, state, query.message.chat.id,
        _render_clear_all_text(), _clear_all_keyboard(),
    )


# --------------------------------------------------------------------------
# Remove Change screen
# --------------------------------------------------------------------------

@router.callback_query(RearrangeStates.waiting_for_remove_change, F.data.startswith(REARR_CB_REMOVE_ITEM_PREFIX))
async def pdf_rearrange_remove_item(query: CallbackQuery, state: FSMContext):
    await query.answer()
    idx_str = query.data[len(REARR_CB_REMOVE_ITEM_PREFIX):]
    try:
        idx = int(idx_str)
    except ValueError:
        return
    data = await state.get_data()
    queue = list(data.get("rearr_queue", []))
    if 0 <= idx < len(queue):
        queue.pop(idx)
        await state.update_data(rearr_queue=queue)
    await _show_pending_changes(query.bot, state, query.message.chat.id)


# --------------------------------------------------------------------------
# Clear All confirm
# --------------------------------------------------------------------------

@router.callback_query(RearrangeStates.waiting_for_clear_all_confirm, F.data == REARR_CB_CLEAR_ALL_YES)
async def pdf_rearrange_clear_all_yes(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(rearr_queue=[])
    await _show_main_menu(query.bot, state, query.message.chat.id)


# --------------------------------------------------------------------------
# Open Ready screen (from Change Added or Pending Changes)
# --------------------------------------------------------------------------

@router.callback_query(
    StateFilter(RearrangeStates.waiting_for_change_added, RearrangeStates.waiting_for_pending_changes),
    F.data == REARR_CB_OPEN_READY,
)
async def pdf_rearrange_open_ready(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    queue = data.get("rearr_queue", [])
    filename = data.get("rearr_filename", "document.pdf")
    await state.set_state(RearrangeStates.waiting_for_ready)
    await _replace_rearr_message(
        query.bot, state, query.message.chat.id,
        _render_ready_text(filename, queue), _ready_keyboard(),
    )


# --------------------------------------------------------------------------
# Ready screen -> Apply -> Processing -> Completion
# --------------------------------------------------------------------------

@router.callback_query(RearrangeStates.waiting_for_ready, F.data == REARR_CB_APPLY)
async def pdf_rearrange_apply(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    chat_id = query.message.chat.id
    data = await state.get_data()
    path = data.get("rearr_input_path")
    queue = data.get("rearr_queue", [])
    filename = data.get("rearr_filename", "document.pdf")

    if not path or not queue:
        await query.message.answer("Session expired, please start over.")
        await _rearr_full_cleanup(state, chat_id)
        return

    await _replace_rearr_message(query.bot, state, chat_id, "⏳ Rearranging pages...\n\nPlease wait...")

    try:
        output_path = await _rearrange_apply(path, queue)
    except Exception as e:
        await _fail(query.message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)

    cleanup_paths = [path, output_path]
    try:
        await query.bot.send_document(
            chat_id,
            FSInputFile(output_path, filename=filename),
            caption=_render_completion_caption(filename, len(queue)),
        )
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)

    status_data = await state.get_data()
    old_chat_id = status_data.get("rearr_status_chat_id")
    old_message_id = status_data.get("rearr_status_message_id")
    await state.clear()
    if old_chat_id is not None and old_message_id is not None:
        try:
            await query.bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception as e:
            logger.debug(f"Rearrange: could not delete processing message: {e}")

    logger.info(f"Rearrange: completed for chat {chat_id}")


# --------------------------------------------------------------------------
# Back (context-aware: goes to the immediately previous step)
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_REARRANGE_STATES), F.data == REARR_CB_BACK)
async def pdf_rearrange_back(query: CallbackQuery, state: FSMContext):
    await query.answer()
    current_state = await state.get_state()
    chat_id = query.message.chat.id

    if current_state == RearrangeStates.waiting_for_move_page_number.state:
        await _show_main_menu(query.bot, state, chat_id)

    elif current_state == RearrangeStates.waiting_for_move_range_input.state:
        await _show_main_menu(query.bot, state, chat_id)

    elif current_state == RearrangeStates.waiting_for_swap_input.state:
        await _show_main_menu(query.bot, state, chat_id)

    elif current_state == RearrangeStates.waiting_for_destination_choice.state:
        data = await state.get_data()
        kind = data.get("rearr_pending_kind")
        if kind == "move_page":
            await state.set_state(RearrangeStates.waiting_for_move_page_number)
            await _replace_rearr_message(
                query.bot, state, chat_id,
                _render_move_page_number_text(), _back_cancel_keyboard(),
            )
        else:
            await state.set_state(RearrangeStates.waiting_for_move_range_input)
            await _replace_rearr_message(
                query.bot, state, chat_id,
                _render_move_range_text(), _back_cancel_keyboard(),
            )

    elif current_state == RearrangeStates.waiting_for_destination_number.state:
        await state.set_state(RearrangeStates.waiting_for_destination_choice)
        data = await state.get_data()
        await _replace_rearr_message(
            query.bot, state, chat_id,
            _render_destination_choice_text(data.get("rearr_pending_kind"), data), _destination_choice_keyboard(),
        )

    elif current_state == RearrangeStates.waiting_for_pending_changes.state:
        await _show_main_menu(query.bot, state, chat_id)

    elif current_state == RearrangeStates.waiting_for_remove_change.state:
        await _show_pending_changes(query.bot, state, chat_id)

    elif current_state == RearrangeStates.waiting_for_clear_all_confirm.state:
        await _show_pending_changes(query.bot, state, chat_id)

    elif current_state == RearrangeStates.waiting_for_ready.state:
        await _show_pending_changes(query.bot, state, chat_id)


# --------------------------------------------------------------------------
# Cancel -- exits the tool completely from any state (no confirmation, per
# spec: Cancel must clear FSM, delete uploaded PDF, delete queued
# operations, delete temp files/messages, and return Home).
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_REARRANGE_STATES), F.data == REARR_CB_CANCEL)
async def pdf_rearrange_cancel(query: CallbackQuery, state: FSMContext):
    await query.answer()
    chat_id = query.message.chat.id
    await _rearr_full_cleanup(state, chat_id)
    await query.message.edit_text(
        "📄 PDF Toolkit -- choose an operation:",
        reply_markup=get_pdf_menu(),
    )


# --------------------------------------------------------------------------
# Stale-button safety net -- same rationale as Rotate/Extract's: if /start
# or another flow's Back/Home/Cancel has already cleared the FSM state, an
# old Rearrange inline keyboard still on screen would otherwise spin
# forever with no reply when pressed.
# --------------------------------------------------------------------------

_REARR_ALL_CALLBACK_PREFIXES = (
    REARR_CB_CANCEL, REARR_CB_UPLOAD_ANOTHER, REARR_CB_MOVE_PAGE, REARR_CB_MOVE_RANGE,
    REARR_CB_SWAP, REARR_CB_REVERSE, REARR_CB_PENDING, REARR_CB_DEST_BEFORE, REARR_CB_DEST_AFTER,
    REARR_CB_DEST_START, REARR_CB_DEST_END, REARR_CB_ADD_CHANGE, REARR_CB_OPEN_READY, REARR_CB_APPLY,
    REARR_CB_REMOVE_CHANGE_MENU, REARR_CB_REMOVE_ITEM_PREFIX, REARR_CB_CLEAR_ALL, REARR_CB_CLEAR_ALL_YES,
    REARR_CB_BACK,
)


@router.callback_query(
    StateFilter(None),
    F.data.startswith("pdfrearr:"),
)
async def pdf_rearrange_stale_callback(query: CallbackQuery):
    await query.answer("This session has expired. Please start again from the menu.", show_alert=True)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logger.debug(f"Rearrange: could not strip keyboard from stale callback message: {e}")
