import asyncio
import time
from functools import wraps
from typing import Callable, Any, Optional
from core.logger import logger


def log_execution_time(func: Callable) -> Callable:
    """Log execution time of async function."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            result = await func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            logger.debug(f"{func.__name__} executed in {elapsed:.3f}s")
            return result
        except Exception as e:
            elapsed = time.perf_counter() - start
            logger.error(f"{func.__name__} failed after {elapsed:.3f}s: {e}")
            raise
    return wrapper


def async_retry(max_retries: int = 3, delay: float = 1.0, exceptions: tuple = (Exception,)) -> Callable:
    """Retry async function on specified exceptions."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    logger.warning(f"Retry {attempt}/{max_retries} for {func.__name__}: {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(delay * attempt)
            raise last_exception
        return wrapper
    return decorator
  
