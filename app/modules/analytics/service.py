from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.repository import RevenueRepository
from app.modules.analytics.schemas import CurrencyRevenue, Grouping, RevenuePeriod, RevenueReport
from app.modules.users.models import User


class InvalidRevenueRangeError(Exception):
    pass


class RevenueService:
    def __init__(self, session: AsyncSession):
        self.repository = RevenueRepository(session)

    async def report(
        self,
        user: User,
        date_from: datetime | None,
        date_to: datetime | None,
        currency: str | None,
        *,
        group_by: Grouping | None = None,
        campaign: str | None = None,
        source: str | None = None,
        referral_code: str | None = None,
    ) -> RevenueReport:
        date_from = date_from.astimezone(UTC) if date_from else None
        date_to = date_to.astimezone(UTC) if date_to else None
        if date_from and date_to and date_from >= date_to:
            raise InvalidRevenueRangeError
        rows = await self.repository.report(
            user,
            date_from,
            date_to,
            currency,
            group_by=group_by,
            campaign=campaign,
            source=source,
            referral_code=referral_code,
        )
        totals = []
        periods = {}
        for row in rows:
            item = CurrencyRevenue.model_validate(row._mapping)
            if group_by and row.period_start is not None:
                periods.setdefault(row.period_start.astimezone(UTC), []).append(item)
            else:
                totals.append(item)
        return RevenueReport(
            group_by=group_by,
            periods=[RevenuePeriod(period_start=k, items=v) for k, v in periods.items()],
            date_from=date_from,
            date_to=date_to,
            items=totals,
        )
