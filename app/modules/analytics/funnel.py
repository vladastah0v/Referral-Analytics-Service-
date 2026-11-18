from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, computed_field
from sqlalchemy import DateTime, cast, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.schemas import Grouping
from app.modules.leads.models import Lead
from app.modules.leads.repository import lead_visibility
from app.modules.referrals.models import ReferralAttribution, ReferralClick, ReferralCode
from app.modules.users.models import User


class FunnelMetrics(BaseModel):
    total_leads: int
    qualified_leads: int
    converted_leads: int

    @computed_field
    @property
    def lead_conversion_rate(self) -> Decimal | None:
        if not self.total_leads:
            return None
        return (Decimal(self.converted_leads) * 100 / self.total_leads).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


class FunnelPeriod(FunnelMetrics):
    period_start: datetime


class FunnelReport(FunnelMetrics):
    date_from: datetime | None
    date_to: datetime | None
    group_by: Grouping | None
    periods: list[FunnelPeriod]


async def funnel_report(
    session: AsyncSession,
    user: User,
    date_from: datetime | None,
    date_to: datetime | None,
    group_by: Grouping | None,
    source: str | None,
    campaign: str | None,
    referral_code: str | None,
) -> FunnelReport:
    start = date_from.astimezone(UTC) if date_from else None
    end = date_to.astimezone(UTC) if date_to else None
    period = (
        func.date_trunc(group_by, Lead.created_at, "UTC", type_=DateTime(timezone=True))
        if group_by
        else cast(literal(None), DateTime(timezone=True))
    )
    qualified = func.count(Lead.qualified_at)
    converted = func.count(Lead.converted_at)
    if end:
        qualified = qualified.filter(Lead.qualified_at < end)
        converted = converted.filter(Lead.converted_at < end)
    statement = select(period, func.count(Lead.id), qualified, converted).where(
        lead_visibility(user)
    )
    if start:
        statement = statement.where(Lead.created_at >= start)
    if end:
        statement = statement.where(Lead.created_at < end)
    if source is not None:
        statement = statement.where(Lead.source == source)
    if campaign is not None or referral_code is not None:
        statement = statement.join(
            ReferralAttribution, ReferralAttribution.id == Lead.referral_attribution_id
        )
    if campaign is not None:
        statement = statement.join(
            ReferralClick, ReferralClick.id == ReferralAttribution.click_id
        ).where(ReferralClick.campaign == campaign)
    if referral_code is not None:
        statement = statement.join(
            ReferralCode, ReferralCode.id == ReferralAttribution.referral_code_id
        ).where(ReferralCode.code == referral_code)
    if group_by:
        statement = statement.group_by(func.grouping_sets(tuple_(period), tuple_())).order_by(
            period
        )
    periods = []
    for bucket, total, qualified_count, converted_count in await session.execute(statement):
        counts = dict(
            total_leads=total, qualified_leads=qualified_count, converted_leads=converted_count
        )
        if bucket is None:
            totals = counts
        else:
            periods.append(FunnelPeriod(period_start=bucket.astimezone(UTC), **counts))
    return FunnelReport(date_from=start, date_to=end, group_by=group_by, periods=periods, **totals)
