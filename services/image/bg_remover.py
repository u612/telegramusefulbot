"""Background removal via rembg (u2net model).

This is the heaviest image operation: the model is ~100MB+, downloaded on
first use (requires outbound network access from the container) and cached
in memory afterward. A semaphore caps how many of these can run at once so
concurrent users can't OOM the container, and the actual removal always
runs in a worker thread since it's CPU-bound and blocking.
"""
import asyncio
import threading

from core.config import settings
from core.logger import logger
from utils.tempfiles import new_temp_path
from services.image._common import ImageProcessingError, open_image

_session = None
_session_lock = threading.Lock()
_semaphore: "asyncio.Semaphore | None" = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_HEAVY_JOBS)
    return _semaphore


def _get_session():
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is None:
            from rembg import new_session
            _session = new_session("u2net")
        return _session


class BackgroundRemover:
    async def remove_background(self, input_path: str) -> str:
        # Validate readability/size up front, outside the semaphore, so a
        # bad file doesn't occupy a heavy-job slot.
        img = open_image(input_path)
        img.close()

        async with _get_semaphore():
            return await asyncio.to_thread(self._remove_sync, input_path)

    @staticmethod
    def _remove_sync(input_path: str) -> str:
        try:
            from rembg import remove
        except ImportError:
            raise ImageProcessingError("Background removal is currently unavailable on this server.")

        try:
            session = _get_session()
            with open(input_path, "rb") as f:
                input_bytes = f.read()
            output_bytes = remove(input_bytes, session=session)
        except ImageProcessingError:
            raise
        except Exception as e:
            raise ImageProcessingError(f"Background removal failed: {e}")

        output_path = new_temp_path(suffix=".png")
        with open(output_path, "wb") as f:
            f.write(output_bytes)
        logger.info(f"Removed background from {input_path} -> {output_path}")
        return output_path
