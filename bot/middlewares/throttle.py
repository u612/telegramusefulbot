from typing import Callable, Dict, Any, Awaitable
import time

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from cachetools import TTLCache

from core.logger import logger


class ThrottlingMiddleware(BaseMiddleware):
    """Simple per-user rate limiting. Silently dropping updates (the
    original behavior) leaves the user thinking their message vanished;
    this now tells them briefly instead.
    """

    def __init__(self, rate_seconds: float = 1.0):
        self.rate_seconds = rate_seconds
        self.cache = TTLCache(maxsize=10000, ttl=max(rate_seconds * 2, 2))

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user_id = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
        if user_id is None:
            return await handler(event, data)

        last_time = self.cache.get(user_id, 0)
        now = time.time()
        if now - last_time < self.rate_seconds:
            logger.debug(f"Throttling user {user_id}")
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("Please slow down a little.")
                except Exception:
                    pass
            # For messages we deliberately stay silent on every throttle hit
            # (e.g. rapid multi-file uploads during Merge) to avoid spamming
            # the chat -- the per-file "Added X/Y" reply already gives
            # feedback once the burst clears.
            return
        self.cache[user_id] = now

        return await handler(event, data)
