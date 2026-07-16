"""Handler for the Settings screen: current effective limits (personalized
per user, and unlimited for the owner) and, if available, the user's usage
count (wired up via UserRepository.increment_usage, which every processing
handler calls on success).
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery

from bot.keyboards.common import back_home_cancel
from core.constants import CB_SETTINGS
from utils.limits import get_effective_limits, format_limit

router = Router()


@router.callback_query(F.data == CB_SETTINGS)
async def settings_callback(query: CallbackQuery, db_user=None):
    limits = get_effective_limits(query.from_user.id, db_user)

    if limits.unlimited:
        lines = [
            "⚙ Settings",
            "",
            "👑 You're the bot owner -- every limit is unlimited on every operation.",
        ]
    else:
        lines = [
            "⚙ Settings -- your current limits",
            "",
            f"Max file size: {limits.file_size // (1024 * 1024)} MB",
            f"Max files per batch (Image→PDF): {format_limit(limits.batch_limit)}",
            f"Merge PDF queue: {format_limit(limits.pdf_queue_limit)}",
            f"Archive Compress: {format_limit(limits.archive_compress_limit)} files",
            f"Archive Extract (before bundling into one zip): {format_limit(limits.archive_extract_return_limit)} files",
            f"Image → PDF: {format_limit(limits.image_to_pdf_limit)} images",
            f"PDF Split: {format_limit(limits.pdf_split_limit)} groups",
        ]

    if db_user is not None:
        lines.append("")
        lines.append(f"Files processed so far: {db_user.usage_count}")

    await query.message.edit_text("\n".join(lines), reply_markup=back_home_cancel())
    await query.answer()
