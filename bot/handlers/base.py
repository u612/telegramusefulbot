from aiogram import Router
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from typing import Optional

from bot.keyboards.main import get_main_menu
from bot.handlers.pdf import cancel_pending_merge_batch
from core.constants import CB_BACK, CB_HOME, CB_CANCEL
from core.logger import logger
from utils.tempfiles import cleanup_tracked_files

router = Router()


async def _reset_to_main_menu(state: FSMContext, chat_id: Optional[int] = None) -> None:
    """Delete any temp files tracked for the current flow, then clear FSM
    state. Centralized here so every exit point (Back/Home/Cancel/new
    /start) goes through the same cleanup instead of each handler having
    to remember to do it -- this is what fixes the temp-file leak that
    happened whenever a user abandoned a flow instead of finishing it.

    Also cancels any pending Merge batch-finalize task for this chat (see
    bot.handlers.pdf) so an abandoned merge queue can't have its status
    message edited by a background task after the flow has ended.
    """
    if chat_id is not None:
        cancel_pending_merge_batch(chat_id)
    await cleanup_tracked_files(state)
    await state.clear()


@router.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    await _reset_to_main_menu(state, message.chat.id)
    await message.answer(
        "Welcome to the Ultimate Telegram File Toolkit Bot!\n"
        "Choose an option:",
        reply_markup=get_main_menu(),
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await _reset_to_main_menu(state, message.chat.id)
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
    await _reset_to_main_menu(state, query.message.chat.id)
    await query.message.edit_text(
        "Choose an option:",
        reply_markup=get_main_menu(),
    )
    await query.answer()


@router.callback_query(lambda c: c.data == CB_HOME)
async def home_callback(query: CallbackQuery, state: FSMContext):
    await _reset_to_main_menu(state, query.message.chat.id)
    await query.message.edit_text(
        "Choose an option:",
        reply_markup=get_main_menu(),
    )
    await query.answer()


@router.callback_query(lambda c: c.data == CB_CANCEL)
async def cancel_callback(query: CallbackQuery, state: FSMContext):
    await _reset_to_main_menu(state, query.message.chat.id)
    await query.message.edit_text(
        "Operation cancelled. Choose an option:",
        reply_markup=get_main_menu(),
    )
    await query.answer()
