"""Repository for the single-row BotConfig table (runtime toggles that must
survive a restart, currently just the userbot on/off switch).
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.bot_config import BotConfig
from core.logger import logger

_SINGLETON_ID = 1


class BotConfigRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_or_create_row(self) -> BotConfig:
        stmt = select(BotConfig).where(BotConfig.id == _SINGLETON_ID)
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            row = BotConfig(id=_SINGLETON_ID, userbot_enabled=False)
            self.session.add(row)
            await self.session.commit()
        return row

    async def is_userbot_enabled(self) -> bool:
        try:
            row = await self._get_or_create_row()
            return bool(row.userbot_enabled)
        except Exception:
            await self.session.rollback()
            logger.exception("Failed to read userbot_enabled flag; defaulting to disabled.")
            return False

    async def set_userbot_enabled(self, enabled: bool) -> None:
        try:
            row = await self._get_or_create_row()
            row.userbot_enabled = enabled
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            logger.exception("Failed to persist userbot_enabled flag.")
            raise
