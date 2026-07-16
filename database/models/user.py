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

    # --- Centralized per-user limit overrides (see utils.limits) ---
    # Every column below is NULLABLE and defaults to NULL, meaning "no
    # personal override -- fall back to the matching core.config.settings
    # default". Raised permanently via the owner-only
    # /upgrade <user_id> <feature> <limit> command (utils.limits.FEATURE_LIMITS
    # maps feature name -> column). The bot owner ignores every one of these
    # columns entirely (unlimited) -- see utils.permissions.is_owner and
    # utils.limits.get_effective_limits.
    #
    # Kept non-nullable with a "20" server default for backward compatibility
    # with rows created before the centralized limit system existed.
    pdf_queue_limit = Column(Integer, nullable=False, default=20, server_default="20")

    file_size_limit = Column(Integer, nullable=True)              # bytes; None = use settings.MAX_FILE_SIZE
    batch_limit = Column(Integer, nullable=True)                  # None = use settings.MAX_FILES_PER_BATCH
    archive_compress_limit = Column(Integer, nullable=True)       # None = use settings.DEFAULT_ARCHIVE_COMPRESS_LIMIT
    archive_extract_return_limit = Column(Integer, nullable=True) # None = use settings.DEFAULT_ARCHIVE_EXTRACT_RETURN_LIMIT
    image_to_pdf_limit = Column(Integer, nullable=True)           # None = use settings.DEFAULT_IMAGE_TO_PDF_LIMIT
    pdf_split_limit = Column(Integer, nullable=True)              # None = use settings.DEFAULT_PDF_SPLIT_LIMIT

    def __repr__(self):
        return f"<User {self.telegram_id}>"
        
