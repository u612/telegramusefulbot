"""Add Password: upload -> enter password -> confirm password -> (optional
weak-password warning) -> permission option -> (optional configure
permissions) -> Summary -> Apply. Mirrors the project's Watermark tool UX
exactly: a single, repeatedly-edited bot message drives the whole flow,
uploads and user input are deleted as soon as they're consumed, validation
errors are temporary, Back is context-aware, and Cancel asks for
confirmation before discarding anything.

Remove Password: upload the (encrypted) file first, then send its current
password. Remove Password deliberately bypasses `_download_and_validate`'s
usual PdfReader-based validation, because `open_pdf_reader()` rejects
already-encrypted PDFs by default -- exactly the files this flow needs to
accept. Basic extension/size checks are still applied by hand. (Unchanged.)
"""
import asyncio
import re
from typing import Dict, List, Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.states.pdf import PDFStates
from bot.keyboards.pdf import get_pdf_menu, PDF_ADD_PASSWORD, PDF_REMOVE_PASSWORD
from bot.keyboards.common import back_home_cancel
from core.constants import SUPPORTED_PDF_EXTS, MERGE_BATCH_FINALIZE_DELAY_SECONDS
from core.logger import logger
from utils.limits import get_effective_limits

from services.pdf._common import PDFProcessingError, open_pdf_reader
from services.pdf.password import PDFPassword

from utils.tempfiles import (
    new_temp_path,
    track_temp_file,
    untrack_temp_files,
    delete_paths,
    get_tracked_files,
)
from utils.validators import validate_extension
from utils.session_manager import register_stale_callbacks

from .common import _PDF_MIME, _download_and_validate, _fail, _track_usage, _finish_with_document, _display_name

router = Router()

# ==========================================================================
# Add Password
# ==========================================================================

# --------------------------------------------------------------------------
# FSM (self-contained StatesGroup, same convention as Watermark -- nothing
# outside this file needs to know about it, and the generic /start reset in
# bot.handlers.base works on tracked temp files + state.clear() regardless
# of which StatesGroup a state belongs to.)
# --------------------------------------------------------------------------


class AddPasswordStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_password = State()
    waiting_for_confirm = State()
    waiting_for_weak_warning = State()
    waiting_for_permission_option = State()
    waiting_for_permissions = State()
    waiting_for_summary = State()
    waiting_for_cancel_confirm = State()


_ALL_PWD_STATES = (
    AddPasswordStates.waiting_for_file,
    AddPasswordStates.waiting_for_password,
    AddPasswordStates.waiting_for_confirm,
    AddPasswordStates.waiting_for_weak_warning,
    AddPasswordStates.waiting_for_permission_option,
    AddPasswordStates.waiting_for_permissions,
    AddPasswordStates.waiting_for_summary,
    AddPasswordStates.waiting_for_cancel_confirm,
)

# --------------------------------------------------------------------------
# Callback data
# --------------------------------------------------------------------------

PWD_CB_CANCEL = "pdfpwd:cancel"
PWD_CB_CANCEL_YES = "pdfpwd:cancel_yes"
PWD_CB_CANCEL_NO = "pdfpwd:cancel_no"
PWD_CB_BACK = "pdfpwd:back"

PWD_CB_WEAK_CONTINUE = "pdfpwd:weak_continue"
PWD_CB_WEAK_EDIT = "pdfpwd:weak_edit"

PWD_CB_PERM_NONE = "pdfpwd:perm_none"
PWD_CB_PERM_CONFIGURE = "pdfpwd:perm_configure"
PWD_CB_PERM_TOGGLE_PREFIX = "pdfpwd:ptoggle:"
PWD_CB_PERM_CONTINUE = "pdfpwd:perm_continue"

PWD_CB_SHOW_PASSWORD = "pdfpwd:show_pwd"
PWD_CB_HIDE_PASSWORD = "pdfpwd:hide_pwd"
PWD_CB_SUM_EDIT_PASSWORD = "pdfpwd:sum_edit_pwd"
PWD_CB_SUM_PERMISSIONS = "pdfpwd:sum_perms"
PWD_CB_APPLY = "pdfpwd:apply"

register_stale_callbacks(prefix="pdfpwd:")

# Buffers for the case a PDF arrives as part of a Telegram media group
# (album) -- Add Password only ever accepts ONE PDF, so a whole album must
# be rejected as a unit. Mirrors Watermark/Rearrange's buffer-then-debounce
# approach.
_pwd_pending_groups: Dict[str, List[Message]] = {}
_pwd_group_tasks: Dict[str, "asyncio.Task"] = {}

MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 128

_PERM_ORDER = ["print", "copy", "edit", "annotate", "fill_forms", "accessibility"]
_PERM_LABELS = {
    "print": "Printing",
    "copy": "Copying",
    "edit": "Editing",
    "annotate": "Annotations",
    "fill_forms": "Form Filling",
    "accessibility": "Accessibility",
}
_PERM_DEFAULTS = {key: False for key in _PERM_ORDER}  # every permission starts BLOCKED


# --------------------------------------------------------------------------
# Password strength
# --------------------------------------------------------------------------

def _password_strength(password: str) -> str:
    """Reasonable strength heuristic: length + variety of character
    classes used (lowercase, uppercase, digits, symbols).
    """
    length = len(password)
    classes = sum([
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"[0-9]", password)),
        bool(re.search(r"[^a-zA-Z0-9]", password)),
    ])
    if length >= 10 and classes >= 3:
        return "strong"
    if length >= 6 and classes >= 2:
        return "medium"
    return "weak"


_STRENGTH_DISPLAY = {
    "weak": "\U0001F534 \u25b0\u25b1\u25b1\u25b1\u25b1 Weak",
    "medium": "\U0001F7E1 \u25b0\u25b0\u25b0\u25b1\u25b1 Medium",
    "strong": "\U0001F7E2 \u25b0\u25b0\u25b0\u25b0\u25b0 Strong",
}


def _strength_line(password: str) -> str:
    return _STRENGTH_DISPLAY[_password_strength(password)]


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------

def _upload_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="\u274c Cancel", callback_data=PWD_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _cancel_only_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="\u274c Cancel", callback_data=PWD_CB_CANCEL)
    b.adjust(1)
    return b.as_markup()


def _back_cancel_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="\u21a9 Back", callback_data=PWD_CB_BACK)
    b.button(text="\u274c Cancel", callback_data=PWD_CB_CANCEL)
    b.adjust(1, 1)
    return b.as_markup()


def _weak_warning_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="\u2705 Continue", callback_data=PWD_CB_WEAK_CONTINUE)
    b.button(text="\U0001F511 Edit Password", callback_data=PWD_CB_WEAK_EDIT)
    b.button(text="\u274c Cancel", callback_data=PWD_CB_CANCEL)
    b.adjust(1, 1, 1)
    return b.as_markup()


def _permission_option_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="\U0001F6AB No Restrictions", callback_data=PWD_CB_PERM_NONE)
    b.button(text="\u2699 Configure Permissions", callback_data=PWD_CB_PERM_CONFIGURE)
    b.button(text="\u21a9 Back", callback_data=PWD_CB_BACK)
    b.button(text="\u274c Cancel", callback_data=PWD_CB_CANCEL)
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def _permissions_keyboard(permissions: dict):
    b = InlineKeyboardBuilder()
    for key in _PERM_ORDER:
        mark = "\u2611" if permissions.get(key) else "\u2b1c"
        b.button(text=f"{mark} {_PERM_LABELS[key]}", callback_data=f"{PWD_CB_PERM_TOGGLE_PREFIX}{key}")
    b.button(text="\u2705 Continue", callback_data=PWD_CB_PERM_CONTINUE)
    b.button(text="\u21a9 Back", callback_data=PWD_CB_BACK)
    b.button(text="\u274c Cancel", callback_data=PWD_CB_CANCEL)
    b.adjust(2, 2, 2, 1, 1, 1)
    return b.as_markup()


def _summary_keyboard(show_password: bool):
    b = InlineKeyboardBuilder()
    b.button(
        text="\U0001F648 Hide Password" if show_password else "\U0001F441 Show Password",
        callback_data=PWD_CB_HIDE_PASSWORD if show_password else PWD_CB_SHOW_PASSWORD,
    )
    b.button(text="\U0001F511 Edit Password", callback_data=PWD_CB_SUM_EDIT_PASSWORD)
    b.button(text="\u2699 Permissions", callback_data=PWD_CB_SUM_PERMISSIONS)
    b.button(text="\u2705 Apply", callback_data=PWD_CB_APPLY)
    b.button(text="\u274c Cancel", callback_data=PWD_CB_CANCEL)
    b.adjust(2, 1, 1, 1)
    return b.as_markup()


def _cancel_confirm_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="\u2705 Yes, Cancel", callback_data=PWD_CB_CANCEL_YES)
    b.button(text="\u21a9 No, Continue", callback_data=PWD_CB_CANCEL_NO)
    b.adjust(1, 1)
    return b.as_markup()


# --------------------------------------------------------------------------
# Screen text renderers
# --------------------------------------------------------------------------

def _render_upload_text() -> str:
    return "\U0001F512 Add Password\n\nProtect your PDF with a password.\n\nPlease send the PDF."


def _render_password_prompt() -> str:
    return (
        "\u2705 PDF received.\n\n"
        "Enter a password for your PDF.\n\n"
        "Requirements\n\n"
        "\u2022 4\u2013128 characters"
    )


def _render_confirm_text(password: str) -> str:
    return (
        "Confirm your password.\n\n"
        "Please enter your password again.\n\n"
        "\U0001F4AA Strength\n"
        f"{_strength_line(password)}"
    )


def _render_weak_warning_text() -> str:
    return (
        "\u26a0\ufe0f Your password is weak.\n\n"
        "A stronger password is recommended.\n\n"
        "Do you want to continue anyway?"
    )


def _render_permission_option_text() -> str:
    return "Would you like to configure PDF permissions?"


def _render_permissions_text() -> str:
    return (
        "\U0001F6E1 PDF Permissions\n\n"
        "Select the actions you want to allow.\n\n"
        "Tap a permission to enable or disable it."
    )


def _render_permissions_summary_block(restrict: bool, permissions: Optional[dict]) -> str:
    if not restrict or permissions is None:
        return "No Restrictions"
    enabled = [key for key in _PERM_ORDER if permissions.get(key)]
    if not enabled:
        return "Blocked"
    return "\n".join(f"\u2705 {_PERM_LABELS[key]}" for key in enabled)


def _render_summary_text(data: dict) -> str:
    password = data.get("pwd_password", "")
    show_password = bool(data.get("pwd_show_password"))
    password_display = password if show_password else "\u2022" * 12
    filename = _display_name(data.get("pwd_filename", "document.pdf"))
    perms_block = _render_permissions_summary_block(
        bool(data.get("pwd_restrict")), data.get("pwd_permissions")
    )
    return (
        "\U0001F512 Add Password\n"
        "\u2501" * 14 + "\n\n"
        "\U0001F4C4 File\n"
        f"{filename}\n\n"
        "\U0001F511 Password\n"
        f"{password_display}\n\n"
        "\U0001F4AA Strength\n"
        f"{_strength_line(password)}\n\n"
        "\U0001F6E1 Permissions\n"
        f"{perms_block}\n\n"
        + "\u2501" * 14 + "\n\n"
        "Everything looks good.\n\n"
        "Press Apply to protect your PDF."
    )


def _render_cancel_confirm_text() -> str:
    return "Cancel password protection?\n\nAny unsaved progress will be lost."


# --------------------------------------------------------------------------
# Prompt edit/send helper (edit existing bot message whenever possible)
# --------------------------------------------------------------------------

async def _show(bot, state: FSMContext, chat_id: int, text: str, keyboard=None) -> None:
    data = await state.get_data()
    message_id = data.get("pwd_prompt_message_id")
    if message_id is not None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
            return
        except Exception as e:
            logger.debug(f"Add Password: edit failed, sending new prompt: {e}")

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await state.update_data(pwd_prompt_message_id=sent.message_id)


_VALIDATION_ERROR_TTL_SECONDS = 7


async def _send_temp_validation_error(bot, chat_id: int, text: str) -> None:
    try:
        sent = await bot.send_message(chat_id, text)
    except Exception as e:
        logger.debug(f"Add Password: could not send temporary validation error: {e}")
        return

    async def _delete_later():
        await asyncio.sleep(_VALIDATION_ERROR_TTL_SECONDS)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
        except Exception as e:
            logger.debug(f"Add Password: could not auto-delete validation error: {e}")

    asyncio.create_task(_delete_later())


async def _delete_message_silently(message: Message) -> None:
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Add Password: could not delete message: {e}")


async def _full_cleanup(state: FSMContext) -> None:
    files = await get_tracked_files(state)
    if files:
        delete_paths(files)
        await untrack_temp_files(state, files)
    await state.clear()


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# Step 1: entry + PDF upload
# --------------------------------------------------------------------------

@router.callback_query(F.data == PDF_ADD_PASSWORD)
async def pdf_add_password_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(AddPasswordStates.waiting_for_file)
    await query.message.edit_text(_render_upload_text(), reply_markup=_upload_keyboard())
    await state.update_data(pwd_prompt_message_id=query.message.message_id)
    await query.answer()


async def _process_single_pwd_pdf(message: Message, state: FSMContext, db_user=None) -> None:
    doc = message.document
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await _delete_message_silently(message)
        await _send_temp_validation_error(message.bot, message.chat.id, "\u274c Please send a PDF.")
        return

    path = await _download_and_validate(message, state, SUPPORTED_PDF_EXTS, _PDF_MIME, "PDF", db_user=db_user)
    if path is None:
        await _delete_message_silently(message)
        await _send_temp_validation_error(message.bot, message.chat.id, "\u274c Please send a PDF.")
        await _show(message.bot, state, message.chat.id, _render_upload_text(), _upload_keyboard())
        return

    try:
        reader = open_pdf_reader(path)
        _ = len(reader.pages)
    except PDFProcessingError as e:
        await untrack_temp_files(state, [path])
        delete_paths([path])
        await _send_temp_validation_error(message.bot, message.chat.id, f"\u274c {e}")
        await _show(message.bot, state, message.chat.id, _render_upload_text(), _upload_keyboard())
        return

    await state.update_data(pwd_input_path=path, pwd_filename=doc.file_name or "document.pdf")
    await state.set_state(AddPasswordStates.waiting_for_password)

    data = await state.get_data()
    old_prompt_id = data.get("pwd_prompt_message_id")
    if old_prompt_id is not None:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=old_prompt_id)
        except Exception as e:
            logger.debug(f"Add Password: could not delete initial upload prompt: {e}")

    sent = await message.answer(_render_password_prompt(), reply_markup=_cancel_only_keyboard())
    await state.update_data(pwd_prompt_message_id=sent.message_id)


async def _finalize_pwd_media_group(key: str, state: FSMContext, db_user) -> None:
    try:
        await asyncio.sleep(MERGE_BATCH_FINALIZE_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    group = _pwd_pending_groups.pop(key, None)
    _pwd_group_tasks.pop(key, None)
    if not group:
        return
    if await state.get_state() != AddPasswordStates.waiting_for_file.state:
        return

    if len(group) > 1:
        for m in group:
            await _delete_message_silently(m)
        await _send_temp_validation_error(group[0].bot, group[0].chat.id, "\u274c Please send only one PDF.")
        return

    await _process_single_pwd_pdf(group[0], state, db_user=db_user)


@router.message(AddPasswordStates.waiting_for_file, F.document)
async def pdf_add_password_receive(message: Message, state: FSMContext, db_user=None):
    if message.media_group_id:
        key = f"{message.chat.id}:{message.media_group_id}"
        group = _pwd_pending_groups.setdefault(key, [])
        group.append(message)
        old_task = _pwd_group_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        _pwd_group_tasks[key] = asyncio.create_task(
            _finalize_pwd_media_group(key, state, db_user)
        )
        return

    await _process_single_pwd_pdf(message, state, db_user=db_user)


@router.message(AddPasswordStates.waiting_for_file)
async def pdf_add_password_receive_invalid(message: Message):
    await _delete_message_silently(message)
    await _send_temp_validation_error(message.bot, message.chat.id, "\u274c Please send a PDF.")


# --------------------------------------------------------------------------
# Step 2: enter password
# --------------------------------------------------------------------------

@router.message(AddPasswordStates.waiting_for_password, F.text)
async def pdf_add_password_value_received(message: Message, state: FSMContext):
    password = message.text or ""
    await _delete_message_silently(message)

    if not password:
        await _send_temp_validation_error(message.bot, message.chat.id, "\u274c Password cannot be empty.")
        return
    if len(password) < MIN_PASSWORD_LEN:
        await _send_temp_validation_error(
            message.bot, message.chat.id, "\u274c Password must contain at least 4 characters."
        )
        return
    if len(password) > MAX_PASSWORD_LEN:
        await _send_temp_validation_error(message.bot, message.chat.id, "\u274c Password cannot exceed 128 characters.")
        return

    await state.update_data(pwd_password=password)
    await state.set_state(AddPasswordStates.waiting_for_confirm)
    await _show(message.bot, state, message.chat.id, _render_confirm_text(password), _back_cancel_keyboard())


@router.message(AddPasswordStates.waiting_for_password)
async def pdf_add_password_value_invalid(message: Message):
    await _delete_message_silently(message)
    await _send_temp_validation_error(message.bot, message.chat.id, "\u274c Password cannot be empty.")


# --------------------------------------------------------------------------
# Step 3: confirm password
# --------------------------------------------------------------------------

@router.message(AddPasswordStates.waiting_for_confirm, F.text)
async def pdf_add_password_confirm_received(message: Message, state: FSMContext):
    confirm = message.text or ""
    await _delete_message_silently(message)

    data = await state.get_data()
    password = data.get("pwd_password", "")

    if confirm != password:
        await _send_temp_validation_error(
            message.bot, message.chat.id, "\u274c Passwords do not match.\n\nPlease confirm your password again."
        )
        return

    if data.get("pwd_pw_edit_return"):
        await state.update_data(pwd_pw_edit_return=False)
        await state.set_state(AddPasswordStates.waiting_for_summary)
        fresh = await state.get_data()
        await _show(message.bot, state, message.chat.id, _render_summary_text(fresh), _summary_keyboard(bool(fresh.get("pwd_show_password"))))
        return

    await state.update_data(pwd_restrict=False, pwd_permissions=None)
    await state.set_state(AddPasswordStates.waiting_for_permission_option)
    await _show(message.bot, state, message.chat.id, _render_permission_option_text(), _permission_option_keyboard())


@router.message(AddPasswordStates.waiting_for_confirm)
async def pdf_add_password_confirm_invalid(message: Message):
    await _delete_message_silently(message)
    await _send_temp_validation_error(
        message.bot, message.chat.id, "\u274c Passwords do not match.\n\nPlease confirm your password again."
    )


# --------------------------------------------------------------------------
# Step 4: permission option
# --------------------------------------------------------------------------

@router.callback_query(AddPasswordStates.waiting_for_permission_option, F.data == PWD_CB_PERM_NONE)
async def pdf_add_password_perm_none(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(pwd_restrict=False, pwd_permissions=None, pwd_edit_return=False)
    await state.set_state(AddPasswordStates.waiting_for_summary)
    data = await state.get_data()
    await _show(query.bot, state, query.message.chat.id, _render_summary_text(data), _summary_keyboard(bool(data.get("pwd_show_password"))))


@router.callback_query(AddPasswordStates.waiting_for_permission_option, F.data == PWD_CB_PERM_CONFIGURE)
async def pdf_add_password_perm_configure(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    permissions = data.get("pwd_permissions") or dict(_PERM_DEFAULTS)
    await state.update_data(pwd_restrict=True, pwd_permissions=permissions)
    await state.set_state(AddPasswordStates.waiting_for_permissions)
    await _show(query.bot, state, query.message.chat.id, _render_permissions_text(), _permissions_keyboard(permissions))


# --------------------------------------------------------------------------
# Step 5: configure permissions
# --------------------------------------------------------------------------

@router.callback_query(AddPasswordStates.waiting_for_permissions, F.data.startswith(PWD_CB_PERM_TOGGLE_PREFIX))
async def pdf_add_password_perm_toggle(query: CallbackQuery, state: FSMContext):
    await query.answer()
    key = query.data[len(PWD_CB_PERM_TOGGLE_PREFIX):]
    if key not in _PERM_ORDER:
        return
    data = await state.get_data()
    permissions = dict(data.get("pwd_permissions") or _PERM_DEFAULTS)
    permissions[key] = not permissions.get(key, False)
    await state.update_data(pwd_permissions=permissions)
    await _show(query.bot, state, query.message.chat.id, _render_permissions_text(), _permissions_keyboard(permissions))


@router.callback_query(AddPasswordStates.waiting_for_permissions, F.data == PWD_CB_PERM_CONTINUE)
async def pdf_add_password_perm_continue(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(pwd_edit_return=False)
    await state.set_state(AddPasswordStates.waiting_for_summary)
    data = await state.get_data()
    await _show(query.bot, state, query.message.chat.id, _render_summary_text(data), _summary_keyboard(bool(data.get("pwd_show_password"))))


# --------------------------------------------------------------------------
# Summary screen: Show/Hide Password, Edit Password, Permissions, Apply
# --------------------------------------------------------------------------

@router.callback_query(AddPasswordStates.waiting_for_summary, F.data == PWD_CB_SHOW_PASSWORD)
async def pdf_add_password_show(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(pwd_show_password=True)
    data = await state.get_data()
    await _show(query.bot, state, query.message.chat.id, _render_summary_text(data), _summary_keyboard(True))


@router.callback_query(AddPasswordStates.waiting_for_summary, F.data == PWD_CB_HIDE_PASSWORD)
async def pdf_add_password_hide(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(pwd_show_password=False)
    data = await state.get_data()
    await _show(query.bot, state, query.message.chat.id, _render_summary_text(data), _summary_keyboard(False))


@router.callback_query(AddPasswordStates.waiting_for_summary, F.data == PWD_CB_SUM_EDIT_PASSWORD)
async def pdf_add_password_sum_edit_password(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(pwd_pw_edit_return=True)
    await state.set_state(AddPasswordStates.waiting_for_password)
    await _show(query.bot, state, query.message.chat.id, _render_password_prompt(), _cancel_only_keyboard())


@router.callback_query(AddPasswordStates.waiting_for_summary, F.data == PWD_CB_SUM_PERMISSIONS)
async def pdf_add_password_sum_permissions(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(pwd_edit_return=True)
    await state.set_state(AddPasswordStates.waiting_for_permission_option)
    await _show(query.bot, state, query.message.chat.id, _render_permission_option_text(), _permission_option_keyboard())


# --------------------------------------------------------------------------
# Weak-password warning (shown only when Apply is pressed on a Weak
# password; Medium/Strong skip straight to processing)
# --------------------------------------------------------------------------

@router.callback_query(AddPasswordStates.waiting_for_summary, F.data == PWD_CB_APPLY)
async def pdf_add_password_apply(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    data = await state.get_data()
    password = data.get("pwd_password", "")

    if _password_strength(password) == "weak":
        await state.set_state(AddPasswordStates.waiting_for_weak_warning)
        await _show(query.bot, state, query.message.chat.id, _render_weak_warning_text(), _weak_warning_keyboard())
        return

    await _do_apply(query, state, user_repo=user_repo, db_user=db_user)


@router.callback_query(AddPasswordStates.waiting_for_weak_warning, F.data == PWD_CB_WEAK_CONTINUE)
async def pdf_add_password_weak_continue(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    await query.answer()
    await _do_apply(query, state, user_repo=user_repo, db_user=db_user)


@router.callback_query(AddPasswordStates.waiting_for_weak_warning, F.data == PWD_CB_WEAK_EDIT)
async def pdf_add_password_weak_edit(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(pwd_pw_edit_return=True)
    await state.set_state(AddPasswordStates.waiting_for_password)
    await _show(query.bot, state, query.message.chat.id, _render_password_prompt(), _cancel_only_keyboard())


# --------------------------------------------------------------------------
# Processing / Apply
# --------------------------------------------------------------------------

async def _do_apply(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None) -> None:
    chat_id = query.message.chat.id
    data = await state.get_data()
    input_path = data.get("pwd_input_path")

    if not input_path:
        await _full_cleanup(state)
        await query.bot.send_message(chat_id, "Session expired, please start over.")
        await query.bot.send_message(chat_id, "\U0001F4C4 PDF Toolkit -- choose an operation:", reply_markup=get_pdf_menu())
        return

    if data.get("pwd_applying"):
        return
    await state.update_data(pwd_applying=True)

    try:
        await query.bot.edit_message_text(
            "\u23f3 Protecting your PDF...", chat_id=chat_id, message_id=query.message.message_id,
            reply_markup=None,
        )
    except Exception as e:
        logger.debug(f"Add Password: could not edit to processing state: {e}")

    password = data.get("pwd_password", "")
    permissions = data.get("pwd_permissions") if data.get("pwd_restrict") else None
    cleanup_paths = [input_path]

    try:
        output_path = await PDFPassword().add_password(input_path, password, permissions)
    except Exception as e:
        if isinstance(e, PDFProcessingError):
            logger.info(f"Add Password: processing error: {e}")
        else:
            logger.exception(f"Add Password: unexpected processing error: {e}")
        try:
            await query.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        except Exception as del_err:
            logger.debug(f"Add Password: could not delete processing message: {del_err}")
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()
        await query.bot.send_message(chat_id, "\u274c Failed to protect the PDF.\n\nPlease try again.")
        await query.bot.send_message(chat_id, "\U0001F4C4 PDF Toolkit -- choose an operation:", reply_markup=get_pdf_menu())
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    cleanup_paths.append(output_path)

    filename = data.get("pwd_filename", "document.pdf")

    try:
        await query.bot.send_document(chat_id, FSInputFile(output_path, filename=filename))
        success_text = (
            "\u2705 Password added successfully!\n\n"
            "\U0001F4C4 File\n"
            f"{_escape_html(filename)}\n\n"
            "\U0001F511 Password\n"
            f"<tg-spoiler><code>{_escape_html(password)}</code></tg-spoiler>\n\n"
            "\u26a0\ufe0f Keep this password safe.\n\n"
            "You'll need it to open the PDF."
        )
        await query.bot.send_message(chat_id, success_text, parse_mode="HTML")
    finally:
        try:
            await query.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        except Exception as e:
            logger.debug(f"Add Password: could not delete processing message: {e}")
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()

    logger.info(f"Add Password: completed for chat {chat_id}")


# --------------------------------------------------------------------------
# Back (context-aware: goes to the immediately previous step. When editing
# the Permissions section from Summary, Back cancels the edit and returns
# straight to Summary, matching Watermark's convention; password editing
# from Summary has no Back button on Step 2, so this only ever applies to
# the Permissions edit chain.)
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_ALL_PWD_STATES), F.data == PWD_CB_BACK)
async def pdf_add_password_back(query: CallbackQuery, state: FSMContext):
    await query.answer()
    current = await state.get_state()
    chat_id = query.message.chat.id
    data = await state.get_data()

    if current == AddPasswordStates.waiting_for_confirm.state:
        await state.set_state(AddPasswordStates.waiting_for_password)
        await _show(query.bot, state, chat_id, _render_password_prompt(), _cancel_only_keyboard())

    elif current == AddPasswordStates.waiting_for_permission_option.state:
        if data.get("pwd_edit_return"):
            await state.update_data(pwd_edit_return=False)
            await state.set_state(AddPasswordStates.waiting_for_summary)
            await _show(query.bot, state, chat_id, _render_summary_text(data), _summary_keyboard(bool(data.get("pwd_show_password"))))
        else:
            await state.set_state(AddPasswordStates.waiting_for_confirm)
            await _show(query.bot, state, chat_id, _render_confirm_text(data.get("pwd_password", "")), _back_cancel_keyboard())

    elif current == AddPasswordStates.waiting_for_permissions.state:
        if data.get("pwd_edit_return"):
            await state.update_data(pwd_edit_return=False)
            await state.set_state(AddPasswordStates.waiting_for_summary)
            await _show(query.bot, state, chat_id, _render_summary_text(data), _summary_keyboard(bool(data.get("pwd_show_password"))))
        else:
            await state.set_state(AddPasswordStates.waiting_for_permission_option)
            await _show(query.bot, state, chat_id, _render_permission_option_text(), _permission_option_keyboard())


# --------------------------------------------------------------------------
# Cancel -- available from every state, requires confirmation
# --------------------------------------------------------------------------

@router.callback_query(StateFilter(*_ALL_PWD_STATES), F.data == PWD_CB_CANCEL)
async def pdf_add_password_cancel(query: CallbackQuery, state: FSMContext):
    await query.answer()
    current = await state.get_state()
    await state.update_data(pwd_pre_cancel_state=current)
    await state.set_state(AddPasswordStates.waiting_for_cancel_confirm)
    await _show(query.bot, state, query.message.chat.id, _render_cancel_confirm_text(), _cancel_confirm_keyboard())


@router.callback_query(AddPasswordStates.waiting_for_cancel_confirm, F.data == PWD_CB_CANCEL_YES)
async def pdf_add_password_cancel_yes(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await _full_cleanup(state)
    await query.message.edit_text("\U0001F4C4 PDF Toolkit -- choose an operation:", reply_markup=get_pdf_menu())


def _pwd_summary_screen(data: dict):
    return _render_summary_text(data), _summary_keyboard(bool(data.get("pwd_show_password")))


def _pwd_permissions_screen(data: dict):
    permissions = data.get("pwd_permissions") or _PERM_DEFAULTS
    return _render_permissions_text(), _permissions_keyboard(permissions)


_SCREEN_RENDERERS = {
    AddPasswordStates.waiting_for_file.state: lambda d: (_render_upload_text(), _upload_keyboard()),
    AddPasswordStates.waiting_for_password.state: lambda d: (_render_password_prompt(), _cancel_only_keyboard()),
    AddPasswordStates.waiting_for_confirm.state: lambda d: (_render_confirm_text(d.get("pwd_password", "")), _back_cancel_keyboard()),
    AddPasswordStates.waiting_for_permission_option.state: lambda d: (_render_permission_option_text(), _permission_option_keyboard()),
    AddPasswordStates.waiting_for_permissions.state: _pwd_permissions_screen,
    AddPasswordStates.waiting_for_summary.state: _pwd_summary_screen,
}


@router.callback_query(AddPasswordStates.waiting_for_cancel_confirm, F.data == PWD_CB_CANCEL_NO)
async def pdf_add_password_cancel_no(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    prev_state = data.get("pwd_pre_cancel_state") or AddPasswordStates.waiting_for_file.state
    renderer = _SCREEN_RENDERERS.get(prev_state, _SCREEN_RENDERERS[AddPasswordStates.waiting_for_file.state])
    await state.set_state(prev_state)
    text, keyboard = renderer(data)
    await _show(query.bot, state, query.message.chat.id, text, keyboard)


_BUTTON_ONLY_STATES = (
    AddPasswordStates.waiting_for_weak_warning,
    AddPasswordStates.waiting_for_permission_option,
    AddPasswordStates.waiting_for_permissions,
    AddPasswordStates.waiting_for_summary,
    AddPasswordStates.waiting_for_cancel_confirm,
)


@router.message(StateFilter(*_BUTTON_ONLY_STATES))
async def pdf_add_password_button_only_screen_message(message: Message):
    await _delete_message_silently(message)


# ==========================================================================
# Remove Password (unchanged)
# ==========================================================================


@router.callback_query(F.data == PDF_REMOVE_PASSWORD)
async def pdf_remove_password_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(PDFStates.waiting_for_file_password_remove)
    await query.message.edit_text("Send the password-protected PDF file.", reply_markup=back_home_cancel())
    await query.answer()


@router.message(PDFStates.waiting_for_file_password_remove, F.document)
async def pdf_remove_password_receive(message: Message, state: FSMContext, db_user=None):
    doc = message.document
    if doc is None:
        await message.answer("Please send a PDF file as a document.")
        return
    limits = get_effective_limits(message.from_user.id, db_user)
    if not limits.unlimited and doc.file_size and doc.file_size > limits.file_size:
        limit_mb = limits.file_size // (1024 * 1024)
        await message.answer(f"File too large (max {limit_mb} MB).")
        return
    if not validate_extension(doc.file_name or "", SUPPORTED_PDF_EXTS):
        await message.answer("Only .pdf files are supported for this action.")
        return
    path = new_temp_path(suffix=".pdf")
    await track_temp_file(state, path)
    await message.bot.download(doc, destination=path)
    await state.update_data(password_remove_input_path=path)
    await state.set_state(PDFStates.waiting_for_password_remove_value)
    await message.answer("Send the current password for this PDF.", reply_markup=back_home_cancel())


@router.message(PDFStates.waiting_for_password_remove_value, F.text)
async def pdf_remove_password_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    data = await state.get_data()
    path = data.get("password_remove_input_path")
    if not path:
        await message.answer("Session expired, please start over.")
        await state.clear()
        return

    await message.answer("Removing password... please wait.")
    try:
        output_path = await PDFPassword().remove_password(path, message.text)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, output_path)
    await _track_usage(user_repo, db_user)
    await _finish_with_document(message, state, output_path, "unprotected.pdf", [path, output_path])
