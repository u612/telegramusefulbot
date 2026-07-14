from sqlalchemy import Column, BigInteger, String, Boolean, DateTime, Integer, func
from database.session import Base


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    is_premium = Column(Boolean, default=False)
    language_code = Column(String(10), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    usage_count = Column(Integer, default=0)

    def __repr__(self):
        return f"<User {self.telegram_id}>"
      
