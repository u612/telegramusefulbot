"""Owner-only admin commands.

Every handler here starts with the same `is_owner` guard and replies with a
plain "not authorized" for anyone else, so these commands are invisible in
effect (not just in the menu) to non-owners.
"""
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from core.config import settings
from core.logger import logger
from utils.permissions import is_owner

router = Router()


@router.message(Command("upgrade"))
async def upgrade_command(message: Message, command: CommandObject, user_repo=None):
    if not is_owner(message.from_user.id):
        return

    args = (command.args or "").split()
    if len(args) != 2:
        await message.answer("Usage: /upgrade <user_id> <limit>")
        return

    try:
        target_id = int(args[0])
        limit = int(args[1])
    except ValueError:
        await message.answer("Both <user_id> and <limit> must be integers.")
        return

    if limit <= 0:
        await message.answer("<limit> must be a positive integer.")
        return

    if user_repo is None:
        await message.answer("Database is unavailable right now, please try again shortly.")
        return

    updated = await user_repo.set_pdf_queue_limit(target_id, limit)
    if not updated:
        await message.answer(
            f"No known user with id {target_id} yet (they need to have messaged the bot at least once)."
        )
        return

    logger.info(f"Owner upgraded user {target_id} to a merge queue limit of {limit}.")
    await message.answer(f"✅ User {target_id} can now queue up to {limit} PDFs for Merge.")


@router.message(Command("userbot_on"))
async def userbot_on_command(message: Message, bot_config_repo=None):
    if not is_owner(message.from_user.id):
        return
    if bot_config_repo is None:
        await message.answer("Database is unavailable right now, please try again shortly.")
        return

    if not (settings.USERBOT_API_ID and settings.USERBOT_API_HASH and settings.USERBOT_SESSION_STRING):
        await message.answer(
            "⚠️ Can't enable the userbot: USERBOT_API_ID, USERBOT_API_HASH and "
            "USERBOT_SESSION_STRING must all be set in the environment first."
        )
        return

    await bot_config_repo.set_userbot_enabled(True)
    logger.info("Owner enabled the userbot transport.")
    await message.answer(
        "✅ Userbot transport enabled. Merge files at or above the Bot API's "
        "~19MB download ceiling will now go through it (up to ~200MB)."
    )


@router.message(Command("userbot_off"))
async def userbot_off_command(message: Message, bot_config_repo=None):
    if not is_owner(message.from_user.id):
        return
    if bot_config_repo is None:
        await message.answer("Database is unavailable right now, please try again shortly.")
        return

    await bot_config_repo.set_userbot_enabled(False)
    logger.info("Owner disabled the userbot transport.")
    await message.answer("✅ Userbot transport disabled. Merge now uses the Bot API exclusively.")


@router.message(Command("status"))
async def status_command(message: Message, bot_config_repo=None):
    if not is_owner(message.from_user.id):
        return

    userbot_enabled = False
    if bot_config_repo is not None:
        userbot_enabled = await bot_config_repo.is_userbot_enabled()

    userbot_configured = bool(
        settings.USERBOT_API_ID and settings.USERBOT_API_HASH and settings.USERBOT_SESSION_STRING
    )

    lines = [
        "🤖 Bot status",
        f"Userbot enabled: {'✅ yes' if userbot_enabled else '❌ no'}",
        f"Userbot configured (env vars set): {'✅ yes' if userbot_configured else '❌ no'}",
        f"Default merge queue limit: {settings.DEFAULT_PDF_QUEUE_LIMIT}",
        f"Bot-API safe download ceiling: {settings.BOT_API_SAFE_DOWNLOAD_LIMIT // (1024 * 1024)} MB",
        f"Userbot file size ceiling: {settings.MAX_FILE_SIZE_USERBOT // (1024 * 1024)} MB",
    ]
    await message.answer("\n".join(lines))
