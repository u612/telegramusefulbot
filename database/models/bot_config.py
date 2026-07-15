from sqlalchemy import Column, Integer, Boolean
from database.session import Base


class BotConfig(Base):
    """Single-row table holding bot-wide runtime toggles that must survive a
    restart. Only one row is ever used (id=1) -- see
    database.repositories.BotConfigRepository, which enforces that.
    """
    __tablename__ = "bot_config"

    id = Column(Integer, primary_key=True)
    userbot_enabled = Column(Boolean, nullable=False, default=False, server_default="false")

    def __repr__(self):
        return f"<BotConfig userbot_enabled={self.userbot_enabled}>"
