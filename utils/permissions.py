"""Owner-privilege helper.

The bot owner is identified purely by `settings.OWNER_ID` (an env var), not
by anything stored in the database -- there's exactly one owner and it
never changes at runtime.
"""
from core.config import settings


def is_owner(telegram_id: int) -> bool:
    """True if `telegram_id` is the configured bot owner.

    `OWNER_ID` defaults to 0, which is not a valid Telegram user id, so an
    unset OWNER_ID safely means "nobody is owner" rather than granting
    owner privileges to a user with id 0.
    """
    return settings.OWNER_ID != 0 and telegram_id == settings.OWNER_ID
