from decimal import ROUND_HALF_UP, Decimal, localcontext

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import FinancialEventType, Order, OrderFinancialEvent
from app.modules.referrals.models import ReferralAttribution, ReferralProgram, RewardType
from app.modules.rewards.models import ReferralReward, ReferralRewardHistory, RewardStatus


class RewardAmountOverflow(Exception):
    pass


def calculate_reward(
    kind: RewardType,
    value: Decimal,
    maximum: Decimal | None,
    payment: Decimal,
    balance: Decimal,
) -> Decimal:
    """Recompute from the full ledger, never round individual refund deltas."""
    with localcontext() as context:
        context.prec = 50
        balance = max(balance, Decimal(0))
        if kind == RewardType.PERCENTAGE:
            amount = balance * value / 100
            if maximum is not None:
                amount = min(amount, maximum)
        else:
            initial = min(value, maximum) if maximum is not None else value
            amount = initial * min(balance / payment, Decimal(1))
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount > Decimal("999999999999.99"):
            raise RewardAmountOverflow
        return amount


class RewardService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def reconcile(self, order: Order, operation: OrderFinancialEvent) -> None:
        # Caller holds the order lock; use the same lock order for future admin operations.
        if order.referral_attribution_id is None:
            return
        reward = await self.session.scalar(
            select(ReferralReward).where(ReferralReward.order_id == order.id).with_for_update()
        )
        if reward is None:
            # No retroactive accrual for legacy orders or payments without an eligible program.
            if operation.event_type != FinancialEventType.PAYMENT:
                return
            program = await self.session.scalar(
                select(ReferralProgram)
                .where(
                    ReferralProgram.is_active.is_(True),
                    ReferralProgram.currency == order.currency,
                    ReferralProgram.starts_at <= operation.occurred_at,
                    or_(
                        ReferralProgram.ends_at.is_(None),
                        ReferralProgram.ends_at > operation.occurred_at,
                    ),
                )
                .order_by(ReferralProgram.starts_at.desc(), ReferralProgram.id.desc())
                .limit(1)
                .with_for_update(read=True)
            )
            if program is None:
                return
            attribution = await self.session.get(ReferralAttribution, order.referral_attribution_id)
            reward = ReferralReward(
                referral_program_id=program.id,
                referral_attribution_id=attribution.id,
                order_id=order.id,
                referrer_id=attribution.referrer_id,
                currency=order.currency,
                reward_amount=Decimal(0),
                reward_type_snapshot=program.reward_type,
                reward_value_snapshot=program.reward_value,
                max_reward_snapshot=program.max_reward,
                rounding_mode="ROUND_HALF_UP",
            )
            self.session.add(reward)
            await self.session.flush()
            is_new = True
        else:
            is_new = False
        financial = OrderFinancialEvent
        balance, payment = (
            await self.session.execute(
                select(
                    func.sum(
                        case(
                            (
                                financial.event_type.in_(
                                    [
                                        FinancialEventType.PAYMENT,
                                        FinancialEventType.ADJUSTMENT_CREDIT,
                                    ]
                                ),
                                financial.amount,
                            ),
                            else_=-financial.amount,
                        )
                    ),
                    func.sum(
                        case(
                            (financial.event_type == FinancialEventType.PAYMENT, financial.amount),
                            else_=0,
                        )
                    ),
                ).where(financial.order_id == order.id)
            )
        ).one()
        amount = calculate_reward(
            reward.reward_type_snapshot,
            reward.reward_value_snapshot,
            reward.max_reward_snapshot,
            payment,
            balance,
        )
        if not is_new and amount == reward.reward_amount:
            return
        reward.reward_amount = amount
        reward.status = RewardStatus.PENDING if amount > 0 else RewardStatus.CANCELLED
        reward.approved_at = None
        revision = await self.session.scalar(
            select(func.coalesce(func.max(ReferralRewardHistory.revision), 0)).where(
                ReferralRewardHistory.reward_id == reward.id
            )
        )
        self.session.add(
            ReferralRewardHistory(
                reward_id=reward.id,
                revision=revision + 1,
                reward_amount=amount,
                status=reward.status,
                reason="accrual" if is_new else "financial_recalculation",
                financial_event_id=operation.id,
            )
        )
        await self.session.flush()
