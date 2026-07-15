"""File transport abstraction for Merge.

Two implementations:
- BotAPITransport: the normal path, using aiogram's Bot (download/answer_document).
  Works for anything up to Telegram's public Bot API limits (~20MB download,
  ~50MB upload).
- UserbotTransport: an MTProto (Pyrogram) client logged in as a regular
  user account, used only when the owner has enabled it (see
  BotConfigRepository) and Pyrogram + credentials are available. Supports
  files up to ~200MB (Telegram's own upload ceiling for a regular account
  without a local Bot API server).

get_transport(...) is the only thing Merge's handler code calls; it decides
which implementation to hand back based on file size + whether the userbot
is enabled/configured, so Merge's logic never has to know which transport
it got -- swapping the underlying userbot library later only touches this
file.
"""
import asyncio
from typing import Optional, Protocol

from core.config import settings
from core.logger import logger


class FileTransport(Protocol):
    async def download(self, message, destination: str) -> None:
        """Download the document attached to `message` (an aiogram
        Message) to `destination` on disk."""
        ...

    async def send_document(self, message, path: str, filename: str, caption: Optional[str] = None) -> None:
        """Send the file at `path` back to the same chat as `message`."""
        ...


class BotAPITransport:
    """Default transport: plain aiogram Bot API calls."""

    async def download(self, message, destination: str) -> None:
        await message.bot.download(message.document, destination=destination)

    async def send_document(self, message, path: str, filename: str, caption: Optional[str] = None) -> None:
        from aiogram.types import FSInputFile
        await message.answer_document(FSInputFile(path, filename=filename), caption=caption)


class _UserbotClientHolder:
    """Lazily starts (and reuses) a single Pyrogram client for the process
    lifetime, so we don't open a fresh MTProto connection per file. Started
    on first real use, not at app boot, so a deployment that never enables
    the userbot never pays the connection cost.
    """

    def __init__(self):
        self._client = None
        self._lock = asyncio.Lock()
        self._unavailable_reason: Optional[str] = None

    async def get_client(self):
        if self._unavailable_reason:
            return None
        if self._client is not None:
            return self._client

        async with self._lock:
            if self._client is not None:
                return self._client
            if self._unavailable_reason:
                return None

            if not (settings.USERBOT_API_ID and settings.USERBOT_API_HASH and settings.USERBOT_SESSION_STRING):
                self._unavailable_reason = "USERBOT_API_ID / USERBOT_API_HASH / USERBOT_SESSION_STRING not configured"
                logger.warning(f"Userbot transport unavailable: {self._unavailable_reason}")
                return None

            try:
                from pyrogram import Client
            except ImportError:
                self._unavailable_reason = "pyrogram is not installed"
                logger.warning(f"Userbot transport unavailable: {self._unavailable_reason}")
                return None

            try:
                client = Client(
                    name="userbot",
                    api_id=settings.USERBOT_API_ID,
                    api_hash=settings.USERBOT_API_HASH,
                    session_string=settings.USERBOT_SESSION_STRING,
                    in_memory=True,
                )
                await client.start()
            except Exception:
                self._unavailable_reason = "failed to start Pyrogram client"
                logger.exception("Userbot transport unavailable: failed to start Pyrogram client")
                return None

            logger.info("Userbot (Pyrogram) client started.")
            self._client = client
            return self._client

    async def shutdown(self) -> None:
        if self._client is not None:
            try:
                await self._client.stop()
                logger.info("Userbot (Pyrogram) client stopped.")
            except Exception:
                logger.exception("Error stopping userbot client (non-fatal).")
            self._client = None


_holder = _UserbotClientHolder()


class UserbotTransport:
    """Large-file transport via a Pyrogram userbot session. Falls back to
    the Bot API transport transparently if the client can't be started
    (missing/invalid credentials, pyrogram not installed, connection
    failure) -- Merge never has to special-case that.
    """

    def __init__(self):
        self._fallback = BotAPITransport()

    async def download(self, message, destination: str) -> None:
        client = await _holder.get_client()
        if client is None:
            await self._fallback.download(message, destination)
            return
        try:
            await client.download_media(message.document.file_id, file_name=destination)
        except Exception:
            logger.exception("Userbot download failed, falling back to Bot API for this file.")
            await self._fallback.download(message, destination)

    async def send_document(self, message, path: str, filename: str, caption: Optional[str] = None) -> None:
        client = await _holder.get_client()
        if client is None:
            await self._fallback.send_document(message, path, filename, caption)
            return
        try:
            await client.send_document(
                chat_id=message.chat.id,
                document=path,
                file_name=filename,
                caption=caption or "",
            )
        except Exception:
            logger.exception("Userbot upload failed, falling back to Bot API for this file.")
            await self._fallback.send_document(message, path, filename, caption)


_bot_api_transport = BotAPITransport()
_userbot_transport = UserbotTransport()


async def get_transport(file_size: int, userbot_enabled: bool) -> FileTransport:
    """Pick the transport for a file of `file_size` bytes.

    - Below the Bot API's real download ceiling: always Bot API (faster,
      no extra MTProto session involved).
    - At/above that ceiling: needs the userbot. If it isn't enabled or
      isn't actually startable, `UserbotTransport` itself falls back to
      Bot API per-call (which will then fail Telegram-side for a too-large
      file with a clear error -- Merge's existing size-limit messaging
      handles that).
    """
    if file_size >= settings.BOT_API_SAFE_DOWNLOAD_LIMIT and userbot_enabled:
        return _userbot_transport
    return _bot_api_transport


async def shutdown_userbot() -> None:
    await _holder.shutdown()
