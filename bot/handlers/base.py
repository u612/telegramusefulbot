from aiogram import Router, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from typing import Optional

from bot.keyboards.main import get_main_menu
from bot.handlers.pdf import (
    cancel_pending_merge_batch, get_merge_lock,
    cancel_pending_img2pdf_batch, get_img2pdf_lock,
)
from core.constants import CB_BACK, CB_HOME, CB_CANCEL
from core.logger import logger
from utils.tempfiles import cleanup_tracked_files

router = Router()


async def _reset_to_main_menu(state: FSMContext, chat_id: Optional[int] = None, bot: Optional[Bot] = None) -> None:
    """Delete any temp files tracked for the current flow, then clear FSM
    state. Centralized here so every exit point (Back/Home/Cancel/new
    /start) goes through the same cleanup instead of each handler having
    to remember to do it -- this is what fixes the temp-file leak that
    happened whenever a user abandoned a flow instead of finishing it.

    Also cancels any pending Merge batch-finalize task for this chat (see
    bot.handlers.pdf) so an abandoned merge queue can't have its status
    message edited by a background task after the flow has ended.

    Also strips the inline keyboard off any still-visible Merge status
    message (tracked in FSM data as merge_status_chat_id/message_id).
    Without this, after /start clears the FSM, the old Merge screen is
    still on screen with live-looking buttons -- but since the FSM state
    they depend on is gone, pressing them never matches any handler, so
    Telegram just spins on "loading" forever with no reply. Removing the
    keyboard here means there's nothing left to press.

    Runs under the same per-chat merge lock pdf_merge_receive/Done/the
    filename step use, even for flows that have nothing to do with Merge:
    it's cheap and uncontended when there's no merge in progress, and it's
    what stops Cancel/Back/Home/a fresh /start from racing a file that's
    still mid-download -- without it, a straggling upload could finish and
    write itself into the FSM state a moment *after* this function had
    already cleared it, leaking its temp file and leaving a stray entry
    behind for whatever flow the user opens next.
    """
    async def _strip_stale_merge_keyboard() -> None:
        if bot is None:
            return
        try:
            data = await state.get_data()
        except Exception:
            return
        old_chat_id = data.get("merge_status_chat_id")
        old_message_id = data.get("merge_status_message_id")
        if old_chat_id is None or old_message_id is None:
            return
        try:
            await bot.edit_message_reply_markup(
                chat_id=old_chat_id, message_id=old_message_id, reply_markup=None
            )
        except Exception as e:
            logger.debug(f"Reset: could not strip keyboard from stale Merge message: {e}")

    async def _strip_stale_img2pdf_keyboard() -> None:
        if bot is None:
            return
        try:
            data = await state.get_data()
        except Exception:
            return
        old_chat_id = data.get("img2pdf_status_chat_id")
        old_message_id = data.get("img2pdf_status_message_id")
        if old_chat_id is None or old_message_id is None:
            return
        try:
            await bot.edit_message_reply_markup(
                chat_id=old_chat_id, message_id=old_message_id, reply_markup=None
            )
        except Exception as e:
            logger.debug(f"Reset: could not strip keyboard from stale Image->PDF message: {e}")

    if chat_id is not None:
        async with get_merge_lock(chat_id):
            await _strip_stale_merge_keyboard()
            cancel_pending_merge_batch(chat_id)
            async with get_img2pdf_lock(chat_id):
                await _strip_stale_img2pdf_keyboard()
                cancel_pending_img2pdf_batch(chat_id)
            await cleanup_tracked_files(state)
            await state.clear()
    else:
        await _strip_stale_merge_keyboard()
        await _strip_stale_img2pdf_keyboard()
        await cleanup_tracked_files(state)
        await state.clear()


@router.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    await _reset_to_main_menu(state, message.chat.id, bot=message.bot)
    await message.answer(
        "Welcome to the Ultimate Telegram File Toolkit Bot!\n"
        "Choose an option:",
        reply_markup=get_main_menu(),
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await _reset_to_main_menu(state, message.chat.id, bot=message.bot)
    await message.answer(
        "Operation cancelled. Choose an option:",
        reply_markup=get_main_menu(),
    )


@router.callback_query(lambda c: c.data == CB_BACK)
async def back_callback(query: CallbackQuery, state: FSMContext):
    # NOTE: this is a simplified "Back" that returns to the main menu rather
    # than a true previous-screen navigation stack. A full breadcrumb stack
    # would need to track screen history in FSM data; flagged here so it's
    # a visible, deliberate simplification rather than a silent gap.
    await _reset_to_main_menu(state, query.message.chat.id, bot=query.bot)
    await query.message.edit_text(
        "Choose an option:",
        reply_markup=get_main_menu(),
    )
    await query.answer()


@router.callback_query(lambda c: c.data == CB_HOME)
async def home_callback(query: CallbackQuery, state: FSMContext):
    await _reset_to_main_menu(state, query.message.chat.id, bot=query.bot)
    await query.message.edit_text(
        "Choose an option:",
        reply_markup=get_main_menu(),
    )
    await query.answer()


@router.callback_query(lambda c: c.data == CB_CANCEL)
async def cancel_callback(query: CallbackQuery, state: FSMContext):
    await _reset_to_main_menu(state, query.message.chat.id, bot=query.bot)
    await query.message.edit_text(
        "Operation cancelled. Choose an option:",
        reply_markup=get_main_menu(),
    )
    await query.answer()
