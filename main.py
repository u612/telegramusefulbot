import asyncio
from contextlib import asynccontextmanager

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat, ErrorEvent
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.config import settings
from core.logger import logger
from bot.handlers import (
    base_router,
    pdf_router,
    image_router,
    archive_router,
    document_router,
    ocr_router,
    settings_router,
    owner_router,
)
from bot.middlewares import LoggingMiddleware, DatabaseMiddleware, ThrottlingMiddleware
from database.session import engine, Base, run_light_migrations
from services.security.validator import init_validator
from services.telegram import shutdown_userbot

# Initialize bot and dispatcher
bot = Bot(token=settings.BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Register middlewares (must be attached to each observer separately in aiogram v3)
dp.message.middleware(LoggingMiddleware())
dp.callback_query.middleware(LoggingMiddleware())
dp.message.middleware(DatabaseMiddleware())
dp.callback_query.middleware(DatabaseMiddleware())
dp.message.middleware(ThrottlingMiddleware())
dp.callback_query.middleware(ThrottlingMiddleware())

# Register routers
dp.include_router(base_router)
dp.include_router(pdf_router)
dp.include_router(image_router)
dp.include_router(archive_router)
dp.include_router(document_router)
dp.include_router(ocr_router)
dp.include_router(settings_router)
dp.include_router(owner_router)


@dp.errors()
async def global_error_handler(event: ErrorEvent) -> bool:
    """Catch-all for any exception raised inside a handler/middleware that
    wasn't already handled locally. Users must never see a raw traceback;
    this logs the full exception for developers and, where possible, tells
    the user something went wrong instead of leaving them hanging.
    """
    logger.exception(f"Unhandled exception while processing update: {event.exception}")

    update = event.update
    try:
        if update.message:
            await update.message.answer(
                "Something went wrong processing that. Please try again, "
                "or press /start to return to the main menu."
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "Something went wrong. Please try again.", show_alert=True
            )
    except Exception:
        # Notifying the user is best-effort; never let a failure here mask
        # or re-raise over the original exception.
        logger.exception("Failed to notify user about the unhandled exception.")

    return True  # mark as handled so aiogram doesn't re-raise


_polling_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _polling_task
    logger.info("Starting bot...")

    init_validator()

    await bot.set_my_commands([
        BotCommand(command="start", description="Start the bot"),
        BotCommand(command="cancel", description="Cancel current operation"),
    ])

    if settings.OWNER_ID:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Start the bot"),
                    BotCommand(command="cancel", description="Cancel current operation"),
                    BotCommand(command="upgrade", description="Raise a user's merge queue limit"),
                    BotCommand(command="userbot_on", description="Enable the userbot large-file transport"),
                    BotCommand(command="userbot_off", description="Disable the userbot large-file transport"),
                    BotCommand(command="status", description="Show bot/userbot status"),
                ],
                scope=BotCommandScopeChat(chat_id=settings.OWNER_ID),
            )
        except Exception:
            # Non-fatal: the owner just won't see the extra commands in their
            # menu until they've started a chat with the bot at least once.
            logger.exception("Failed to set owner-scoped bot commands (non-fatal).")

    # Drop any webhook + pending updates left over from a previous deployment
    # before starting long polling, otherwise Telegram returns a 409 Conflict.
    await bot.delete_webhook(drop_pending_updates=True)

    # Optional: create DB tables if they don't exist yet. For production,
    # prefer Alembic migrations; this is a convenience fallback for fresh
    # environments (e.g. first Railway deploy before migrations are wired up).
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await run_light_migrations(conn)
        logger.info("Database tables verified/created.")
    except Exception:
        logger.exception("Database initialization failed -- continuing startup, "
                          "but DB-backed features will error until this is fixed.")

    _polling_task = asyncio.create_task(dp.start_polling(bot))

    def _on_polling_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.critical(f"Polling task crashed: {exc!r}")

    _polling_task.add_done_callback(_on_polling_done)

    yield

    # Shutdown
    logger.info("Shutting down...")
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    await bot.session.close()
    await engine.dispose()
    await shutdown_userbot()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    """Railway health check. Reports the polling task's actual state, not
    just that the web server process is alive -- if polling crashed, this
    now correctly reports unhealthy instead of a false "ok".
    """
    polling_alive = _polling_task is not None and not _polling_task.done()
    status_code = 200 if polling_alive else 503
    return JSONResponse(
        content={"status": "ok" if polling_alive else "degraded", "polling": polling_alive},
        status_code=status_code,
    )


if __name__ == "__main__":
    # Railway injects PORT dynamically; 8000 is only a local-dev fallback
    # (settings.PORT reads the PORT env var if present).
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
