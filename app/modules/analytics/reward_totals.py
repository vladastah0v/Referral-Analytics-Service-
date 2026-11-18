from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import DateTime, func, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.modules.analytics.schemas import Grouping
from app.modules.referrals.models import ReferralAttribution, ReferralClick, ReferralCode
from app.modules.rewards.models import ReferralReward, ReferralRewardHistory


class CurrencyRewards(BaseModel):
    currency: str
    total_rewards: Decimal


async def reward_totals(
    session: AsyncSession,
    start: datetime | None,
    end: datetime | None,
    group_by: Grouping | None,
    campaign: str | None,
    source: str | None,
    referral_code: str | None,
):
    # Each revision contains the resulting balance, so aggregate changes, including reversals.
    h = ReferralRewardHistory
    r = ReferralReward
    earlier = aliased(ReferralRewardHistory)
    previous = (
        select(earlier.reward_amount)
        .where(earlier.reward_id == h.reward_id, earlier.revision < h.revision)
        .order_by(earlier.revision.desc())
        .limit(1)
        .correlate(h)
        .lateral()
    )
    delta = h.reward_amount - func.coalesce(previous.c.reward_amount, 0)
    revisions = (
        select(h.created_at, r.currency, delta.label("delta"))
        .select_from(h)
        .join(r, r.id == h.reward_id)
        .outerjoin(previous, true())
    )
    if start:
        revisions = revisions.where(h.created_at >= start)
    if end:
        revisions = revisions.where(h.created_at < end)
    if campaign is not None or source is not None or referral_code is not None:
        revisions = revisions.join(
            ReferralAttribution, ReferralAttribution.id == r.referral_attribution_id
        )
    if campaign is not None or source is not None:
        revisions = revisions.join(ReferralClick, ReferralClick.id == ReferralAttribution.click_id)
        if campaign is not None:
            revisions = revisions.where(ReferralClick.campaign == campaign)
        if source is not None:
            revisions = revisions.where(ReferralClick.source == source)
    if referral_code is not None:
        revisions = revisions.join(
            ReferralCode, ReferralCode.id == ReferralAttribution.referral_code_id
        ).where(ReferralCode.code == referral_code)
    changes = revisions.subquery().c
    statement = select(changes.currency, func.sum(changes.delta).label("total_rewards")).where(
        changes.delta != 0
    )
    if group_by:
        period = func.date_trunc(group_by, changes.created_at, "UTC", type_=DateTime(timezone=True))
        statement = (
            statement.add_columns(period.label("period_start"))
            .group_by(
                func.grouping_sets(tuple_(changes.currency, period), tuple_(changes.currency))
            )
            .order_by(period, changes.currency)
        )
    else:
        statement = statement.group_by(changes.currency).order_by(changes.currency)
    return (await session.execute(statement)).mappings().all()
