"""Handler for the Settings screen: current limits and, if available, the
user's usage count (wired up via UserRepository.increment_usage, which
every processing handler calls on success).
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery

from bot.keyboards.common import back_home_cancel
from core.constants import CB_SETTINGS
from core.config import settings

router = Router()


@router.callback_query(F.data == CB_SETTINGS)
async def settings_callback(query: CallbackQuery, db_user=None):
    limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
    lines = [
        "⚙ Settings",
        "",
        f"Max file size: {limit_mb} MB",
        f"Max files per batch (Merge/Archive/Image→PDF): {settings.MAX_FILES_PER_BATCH}",
    ]
    if db_user is not None:
        lines.append(f"Files processed so far: {db_user.usage_count}")

    await query.message.edit_text("\n".join(lines), reply_markup=back_home_cancel())
    await query.answer()
