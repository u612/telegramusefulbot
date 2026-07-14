from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from core.logger import logger


class LoggingMiddleware(BaseMiddleware):
    """Log incoming updates."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Log based on event type
        if isinstance(event, Message):
            user = event.from_user
            text = event.text or event.caption or "<media>"
            logger.info(f"Message from {user.id} ({user.username}): {text[:50]}")
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            logger.info(f"Callback from {user.id}: {event.data}")
        return await handler(event, data)
