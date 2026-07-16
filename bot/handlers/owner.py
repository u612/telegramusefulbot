"""Owner-only admin commands.

Every handler here starts with the same `is_owner` guard and replies with
nothing at all for anyone else, so these commands are invisible in effect
(not just in the menu) to non-owners.
"""
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from core.config import settings
from core.logger import logger
from utils.permissions import is_owner
from utils.limits import FEATURE_LIMITS, format_limit

router = Router()

_FEATURE_LIST = "\n".join(f"  • {name}" for name in FEATURE_LIMITS)


@router.message(Command("upgrade"))
async def upgrade_command(message: Message, command: CommandObject, user_repo=None):
    if not is_owner(message.from_user.id):
        return

    args = (command.args or "").split()

    if len(args) != 3:
        await message.answer(
            "Usage: /upgrade <user_id> <feature> <limit>\n\n"
            f"Available features:\n{_FEATURE_LIST}\n\n"
            "Example: /upgrade 123456789 merge 100"
        )
        return

    raw_user_id, feature, raw_limit = args
    feature = feature.strip().lower()

    try:
        target_id = int(raw_user_id)
        limit = int(raw_limit)
    except ValueError:
        await message.answer("Both <user_id> and <limit> must be integers.")
        return

    if limit <= 0:
        await message.answer("<limit> must be a positive integer.")
        return

    if feature not in FEATURE_LIMITS:
        await message.answer(
            f"Unknown feature {feature!r}. Available features:\n{_FEATURE_LIST}"
        )
        return

    if user_repo is None:
        await message.answer("Database is unavailable right now, please try again shortly.")
        return

    updated = await user_repo.set_feature_limit(target_id, feature, limit)
    if not updated:
        await message.answer(
            f"No known user with id {target_id} yet (they need to have messaged the bot at least once)."
        )
        return

    logger.info(f"Owner upgraded user {target_id}'s {feature!r} limit to {limit}.")
    await message.answer(f"✅ User {target_id}'s {feature!r} limit is now {limit}.")


@router.message(Command("limits"))
async def limits_command(message: Message, command: CommandObject, user_repo=None):
    """Owner-only: show a user's current effective limits across every
    feature, so /upgrade's effect can be verified without querying the DB
    by hand.
    """
    if not is_owner(message.from_user.id):
        return

    from utils.limits import get_effective_limits

    args = (command.args or "").split()
    target_id = message.from_user.id
    if args:
        try:
            target_id = int(args[0])
        except ValueError:
            await message.answer("Usage: /limits [user_id]")
            return

    db_user = await user_repo.get_user(target_id) if user_repo is not None else None
    limits = get_effective_limits(target_id, db_user)

    if limits.unlimited:
        await message.answer(f"User {target_id} is the bot owner -- every limit is unlimited.")
        return

    lines = [f"📊 Effective limits for user {target_id}:"]
    lines.append(f"  file_size: {format_limit(limits.file_size)} bytes")
    lines.append(f"  batch: {format_limit(limits.batch_limit)}")
    lines.append(f"  merge: {format_limit(limits.pdf_queue_limit)}")
    lines.append(f"  archive_compress: {format_limit(limits.archive_compress_limit)}")
    lines.append(f"  archive_extract: {format_limit(limits.archive_extract_return_limit)}")
    lines.append(f"  image_to_pdf: {format_limit(limits.image_to_pdf_limit)}")
    lines.append(f"  split: {format_limit(limits.pdf_split_limit)}")
    await message.answer("\n".join(lines))


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
        "",
        "Owner is unrestricted on every operation (file size, batch size, "
        "queue size, timeouts). Use /upgrade <user_id> <feature> <limit> to "
        f"raise a specific user's limits. Features: {', '.join(FEATURE_LIMITS)}.",
    ]
    await message.answer("\n".join(lines))
