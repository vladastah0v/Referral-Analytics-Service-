from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.funnel import FunnelMetrics, funnel_report
from app.modules.analytics.referrals import referral_report
from app.modules.analytics.repository import RevenueRepository
from app.modules.analytics.schemas import CurrencyRevenue, Grouping
from app.modules.referrals.schemas import ReferralMetrics
from app.modules.users.models import User


class OverviewRevenue(CurrencyRevenue):
    average_order_value: Decimal | None


class OverviewMetrics(BaseModel):
    referrals: ReferralMetrics = Field(default_factory=lambda: ReferralMetrics.from_counts(0, 0, 0))
    funnel: FunnelMetrics = Field(
        default_factory=lambda: FunnelMetrics(total_leads=0, qualified_leads=0, converted_leads=0)
    )
    revenue: list[OverviewRevenue] = Field(default_factory=list)


class OverviewPeriod(OverviewMetrics):
    period_start: datetime


class OverviewReport(OverviewMetrics):
    date_from: datetime | None
    date_to: datetime | None
    group_by: Grouping | None
    periods: list[OverviewPeriod]


async def overview_report(
    session: AsyncSession,
    user: User,
    date_from: datetime | None,
    date_to: datetime | None,
    group_by: Grouping | None,
    campaign: str | None,
    source: str | None,
    referral_code: str | None,
) -> OverviewReport:
    referrals = await referral_report(
        session, date_from, date_to, group_by, campaign, source, referral_code
    )
    funnel = await funnel_report(
        session, user, date_from, date_to, group_by, source, campaign, referral_code
    )
    rows = await RevenueRepository(session).report(
        user,
        referrals.date_from,
        referrals.date_to,
        None,
        group_by=group_by,
        campaign=campaign,
        source=source,
        referral_code=referral_code,
    )
    periods: dict[datetime, OverviewPeriod] = {}

    def bucket(start: datetime) -> OverviewPeriod:
        if start not in periods:
            periods[start] = OverviewPeriod(period_start=start)
        return periods[start]

    for period in referrals.periods:
        bucket(period.period_start).referrals = period
    for period in funnel.periods:
        bucket(period.period_start).funnel = period
    totals = []
    for row in rows:
        item = OverviewRevenue.model_validate(row._mapping)
        if group_by and row.period_start is not None:
            bucket(row.period_start.astimezone(UTC)).revenue.append(item)
        else:
            totals.append(item)
    return OverviewReport(
        date_from=referrals.date_from,
        date_to=referrals.date_to,
        group_by=group_by,
        referrals=referrals,
        funnel=funnel,
        revenue=totals,
        periods=[periods[start] for start in sorted(periods)],
    )
