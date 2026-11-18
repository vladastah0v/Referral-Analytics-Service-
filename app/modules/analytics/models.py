from sqlalchemy import BigInteger
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AnalyticsCacheRevision(Base):
    __tablename__ = "analytics_cache_revisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
