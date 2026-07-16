"""Repository for User model operations."""
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.user import User
from core.logger import logger
from utils.limits import FEATURE_LIMITS


class UserRepository:
    """All DB access for the User model goes through here (repository
    pattern) so handlers/middleware never touch SQLAlchemy directly.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(
        self,
        telegram_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        is_premium: bool = False,
        language_code: Optional[str] = None,
    ) -> User:
        """Get the existing user for this telegram_id, updating any changed
        profile fields, or create a new row if none exists.
        """
        try:
            stmt = select(User).where(User.telegram_id == telegram_id)
            result = await self.session.execute(stmt)
            user = result.scalar_one_or_none()

            if user:
                changed = (
                    user.username != username
                    or user.first_name != first_name
                    or user.is_premium != is_premium
                    or user.language_code != language_code
                )
                if changed:
                    user.username = username
                    user.first_name = first_name
                    user.is_premium = is_premium
                    user.language_code = language_code
                    await self.session.commit()
                return user

            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                is_premium=is_premium,
                language_code=language_code,
            )
            self.session.add(user)
            await self.session.commit()
            return user
        except Exception:
            await self.session.rollback()
            logger.exception(f"get_or_create failed for telegram_id={telegram_id}")
            raise

    async def get_user(self, telegram_id: int) -> Optional[User]:
        """Return the full User row (used to build effective limits), or
        None if the user hasn't messaged the bot yet.
        """
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_feature_limit(self, telegram_id: int, feature: str, limit: int) -> bool:
        """Permanently set a user's per-feature limit override (owner-only
        `/upgrade <user_id> <feature> <limit>` command). `feature` must be a
        key in `utils.limits.FEATURE_LIMITS`. Returns False if no such user
        exists yet, or if `feature` is unknown.
        """
        entry = FEATURE_LIMITS.get(feature)
        if entry is None:
            return False
        column_name, _default_attr = entry

        try:
            stmt = (
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(**{column_name: limit})
            )
            result = await self.session.execute(stmt)
            await self.session.commit()
            return result.rowcount > 0
        except Exception:
            await self.session.rollback()
            logger.exception(
                f"set_feature_limit({feature!r}) failed for telegram_id={telegram_id}"
            )
            raise

    async def set_pdf_queue_limit(self, telegram_id: int, limit: int) -> bool:
        """Backward-compatible alias for the original merge-only upgrade
        path. Prefer `set_feature_limit(telegram_id, "merge", limit)`.
        """
        return await self.set_feature_limit(telegram_id, "merge", limit)

    async def get_pdf_queue_limit(self, telegram_id: int) -> Optional[int]:
        """Return the user's stored merge-queue limit, or None if the user
        doesn't exist yet (caller should fall back to the default).
        """
        stmt = select(User.pdf_queue_limit).where(User.telegram_id == telegram_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def increment_usage(self, telegram_id: int) -> None:
        """Atomically increment the usage counter for a user."""
        try:
            stmt = (
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(usage_count=User.usage_count + 1)
            )
            await self.session.execute(stmt)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            logger.exception(f"increment_usage failed for telegram_id={telegram_id}")
            raise
