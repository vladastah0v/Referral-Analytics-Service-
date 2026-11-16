from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.rewards.models import ReferralReward, ReferralRewardHistory, RewardStatus


def reward_record(reward: ReferralReward, revision: int) -> dict:
    return {column.key: getattr(reward, column.key) for column in reward.__table__.columns} | {
        "revision": revision
    }


async def list_rewards(
    session: AsyncSession,
    referrer_id: UUID | None,
    status: RewardStatus | None,
    currency: str | None,
    limit: int,
    offset: int,
) -> list[dict]:
    revision = (
        select(func.coalesce(func.max(ReferralRewardHistory.revision), 0))
        .where(ReferralRewardHistory.reward_id == ReferralReward.id)
        .correlate(ReferralReward)
        .scalar_subquery()
    )
    statement = select(ReferralReward, revision)
    if referrer_id is not None:
        statement = statement.where(ReferralReward.referrer_id == referrer_id)
    if status is not None:
        statement = statement.where(ReferralReward.status == status)
    if currency is not None:
        statement = statement.where(ReferralReward.currency == currency)
    statement = statement.order_by(ReferralReward.created_at.desc(), ReferralReward.id.desc())
    rows = await session.execute(statement.limit(limit).offset(offset))
    return [reward_record(reward, version) for reward, version in rows]
