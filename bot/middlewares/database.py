from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from database.session import AsyncSessionLocal
from database.repositories import UserRepository, BotConfigRepository


class DatabaseMiddleware(BaseMiddleware):
    """Provide database session and user repository to handlers."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with AsyncSessionLocal() as session:
            # Store session in data
            data["db_session"] = session
            data["user_repo"] = UserRepository(session)
            data["bot_config_repo"] = BotConfigRepository(session)

            # Get or create user if event has from_user
            user = None
            if isinstance(event, Message) and event.from_user:
                user = event.from_user
            elif isinstance(event, CallbackQuery) and event.from_user:
                user = event.from_user
            if user:
                repo = data["user_repo"]
                db_user = await repo.get_or_create(
                    telegram_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    is_premium=user.is_premium,
                    language_code=user.language_code,
                )
                data["db_user"] = db_user

            return await handler(event, data)
