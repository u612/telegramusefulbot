import sys
from loguru import logger
from core.config import settings

# Remove default handler
logger.remove()

# Add console handler with level
logger.add(
    sys.stderr,
    level=settings.LOG_LEVEL,
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)

# Add file rotation (optional)
logger.add(
    "logs/bot.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG",
    format="{time} | {level} | {name}:{function}:{line} - {message}",
)

# Expose logger
__all__ = ["logger"]
