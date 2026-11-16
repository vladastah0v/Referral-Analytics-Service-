from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.repository import RevenueRepository
from app.modules.analytics.schemas import CurrencyRevenue, RevenueReport
from app.modules.users.models import User


class InvalidRevenueRangeError(Exception):
    pass


class RevenueService:
    def __init__(self, session: AsyncSession):
        self.repository = RevenueRepository(session)

    async def report(
        self, user: User, date_from: datetime | None, date_to: datetime | None, currency: str | None
    ) -> RevenueReport:
        date_from = date_from.astimezone(UTC) if date_from else None
        date_to = date_to.astimezone(UTC) if date_to else None
        if date_from and date_to and date_from >= date_to:
            raise InvalidRevenueRangeError
        rows = await self.repository.report(user, date_from, date_to, currency)
        return RevenueReport(
            date_from=date_from,
            date_to=date_to,
            items=[CurrencyRevenue.model_validate(row._mapping) for row in rows],
        )
