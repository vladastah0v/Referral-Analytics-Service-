from datetime import UTC, datetime

from sqlalchemy import DateTime, Uuid, cast, func, literal, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.schemas import Grouping
from app.modules.referrals.models import ReferralAttribution, ReferralClick, ReferralCode
from app.modules.referrals.schemas import ReferralMetrics


class ReferralPeriod(ReferralMetrics):
    period_start: datetime


class ReferralReport(ReferralMetrics):
    date_from: datetime | None
    date_to: datetime | None
    group_by: Grouping | None
    periods: list[ReferralPeriod]


async def referral_report(
    session: AsyncSession,
    date_from: datetime | None,
    date_to: datetime | None,
    group_by: Grouping | None,
    campaign: str | None,
    source: str | None,
    referral_code: str | None,
) -> ReferralReport:
    start = date_from.astimezone(UTC) if date_from else None
    end = date_to.astimezone(UTC) if date_to else None
    click, attr = ReferralClick, ReferralAttribution

    def filtered(statement, timestamp):
        if start is not None:
            statement = statement.where(timestamp >= start)
        if end is not None:
            statement = statement.where(timestamp < end)
        if campaign is not None:
            statement = statement.where(click.campaign == campaign)
        if source is not None:
            statement = statement.where(click.source == source)
        if referral_code is not None:
            statement = statement.join(ReferralCode, ReferralCode.id == click.referral_code_id)
            statement = statement.where(ReferralCode.code == referral_code)
        return statement

    clicks = select(click.clicked_at.label("at"), click.visitor_id.label("visitor"))
    registrations = (
        select(attr.attributed_at, cast(literal(None), Uuid))
        .select_from(attr)
        .join(click, click.id == attr.click_id)
    )
    events = union_all(
        filtered(clicks, click.clicked_at), filtered(registrations, attr.attributed_at)
    ).subquery()
    # Clicks always have a visitor; registrations use NULL so they do not inflate reach.
    counts = (
        func.count(events.c.visitor).label("clicks"),
        func.count(func.distinct(events.c.visitor)).label("visitors"),
        (func.count() - func.count(events.c.visitor)).label("registrations"),
    )
    period = (
        func.date_trunc(group_by, events.c.at, "UTC", type_=DateTime(timezone=True))
        if group_by
        else cast(literal(None), DateTime(timezone=True))
    )
    statement = select(period.label("period_start"), *counts)
    if group_by:
        statement = statement.group_by(func.grouping_sets(tuple_(period), tuple_())).order_by(
            period
        )
    periods = []
    for row in await session.execute(statement):
        metrics = ReferralMetrics.from_counts(row.clicks, row.visitors, row.registrations)
        if row.period_start is None:
            totals = metrics
        else:
            periods.append(
                ReferralPeriod(
                    period_start=row.period_start.astimezone(UTC), **metrics.model_dump()
                )
            )
    return ReferralReport(
        date_from=start, date_to=end, group_by=group_by, periods=periods, **totals.model_dump()
    )
