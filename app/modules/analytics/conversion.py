from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.funnel import FunnelReport, funnel_report
from app.modules.analytics.referrals import ReferralReport, referral_report
from app.modules.analytics.schemas import Grouping
from app.modules.users.models import User


class ConversionPeriod(BaseModel):
    referrals: ReferralReport
    funnel: FunnelReport


class ConversionChange(BaseModel):
    percentage_points: Decimal | None
    relative_growth_percent: Decimal | None


class ConversionReport(BaseModel):
    previous: ConversionPeriod
    current: ConversionPeriod
    referral_change: ConversionChange
    lead_change: ConversionChange


def compare_rates(
    old_count: int, old_total: int, new_count: int, new_total: int
) -> ConversionChange:
    # Use the unrounded ratios; small nonzero rates can display as 0.00%.
    if not old_total or not new_total:
        return ConversionChange(percentage_points=None, relative_growth_percent=None)
    difference = Decimal(new_count * old_total - old_count * new_total) * 100
    cents = Decimal("0.01")
    return ConversionChange(
        percentage_points=(difference / (old_total * new_total)).quantize(
            cents, rounding=ROUND_HALF_UP
        ),
        relative_growth_percent=(difference / (old_count * new_total)).quantize(
            cents, rounding=ROUND_HALF_UP
        )
        if old_count
        else None,
    )


async def conversion_report(
    session: AsyncSession,
    user: User,
    start: datetime,
    end: datetime,
    previous_start: datetime,
    previous_end: datetime,
    group_by: Grouping | None,
    campaign: str | None,
    source: str | None,
    referral_code: str | None,
) -> ConversionReport:
    async def period(left, right):
        return ConversionPeriod(
            referrals=await referral_report(
                session, left, right, group_by, campaign, source, referral_code
            ),
            funnel=await funnel_report(
                session, user, left, right, group_by, source, campaign, referral_code
            ),
        )

    old = await period(previous_start, previous_end)
    new = await period(start, end)
    return ConversionReport(
        previous=old,
        current=new,
        referral_change=compare_rates(
            old.referrals.referred_registrations,
            old.referrals.unique_visitors,
            new.referrals.referred_registrations,
            new.referrals.unique_visitors,
        ),
        lead_change=compare_rates(
            old.funnel.converted_leads,
            old.funnel.total_leads,
            new.funnel.converted_leads,
            new.funnel.total_leads,
        ),
    )
