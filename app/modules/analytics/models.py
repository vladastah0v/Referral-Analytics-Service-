from datetime import date, datetime

from sqlalchemy import BigInteger, DateTime, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AnalyticsCacheRevision(Base):
    __tablename__ = "analytics_cache_revisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SnapshotRequest(Base):
    __tablename__ = "analytics_snapshot_requests"
    __table_args__ = (Index("ix_snapshot_requests_day", "day"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[date]


class DailySnapshot(Base):
    __tablename__ = "analytics_daily_snapshots"
    day: Mapped[date] = mapped_column(primary_key=True)
    report: Mapped[dict] = mapped_column(JSONB)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SnapshotFailure(Base):
    __tablename__ = "analytics_snapshot_failures"
    day: Mapped[date] = mapped_column(primary_key=True)
    task_id: Mapped[str]
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
