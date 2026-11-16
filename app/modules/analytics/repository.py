from datetime import datetime

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import FinancialEventType, Order, OrderFinancialEvent
from app.modules.orders.repository import order_visibility
from app.modules.users.models import User


class RevenueRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def report(
        self, user: User, date_from: datetime | None, date_to: datetime | None, currency: str | None
    ):
        event = OrderFinancialEvent
        credit = case(
            (
                event.event_type.in_(
                    [FinancialEventType.PAYMENT, FinancialEventType.ADJUSTMENT_CREDIT]
                ),
                event.amount,
            ),
            else_=0,
        )
        refund = case((event.event_type == FinancialEventType.REFUND, event.amount), else_=0)
        debit = case(
            (event.event_type == FinancialEventType.ADJUSTMENT_DEBIT, event.amount), else_=0
        )
        net = credit - refund - debit
        attributed = case((Order.referral_attribution_id.is_not(None), net), else_=0)
        statement = (
            select(
                event.currency,
                func.count(
                    distinct(case((event.event_type == FinancialEventType.PAYMENT, event.order_id)))
                ).label("paid_orders"),
                func.sum(credit).label("gross_revenue"),
                func.sum(refund).label("refunds"),
                func.sum(debit).label("adjustments_debit"),
                func.sum(net).label("net_revenue"),
                func.sum(attributed).label("referral_attributed_revenue"),
            )
            .join(Order, Order.id == event.order_id)
            .where(order_visibility(user))
        )
        if date_from is not None:
            statement = statement.where(event.occurred_at >= date_from)
        if date_to is not None:
            statement = statement.where(event.occurred_at < date_to)
        if currency is not None:
            statement = statement.where(event.currency == currency)
        return (
            await self.session.execute(statement.group_by(event.currency).order_by(event.currency))
        ).all()
