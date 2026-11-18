from datetime import datetime

from sqlalchemy import DateTime, case, distinct, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.schemas import Grouping
from app.modules.orders.models import FinancialEventType, Order, OrderFinancialEvent
from app.modules.orders.repository import order_visibility
from app.modules.referrals.models import ReferralAttribution, ReferralClick, ReferralCode
from app.modules.users.models import User


class RevenueRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

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
    ):
        event = OrderFinancialEvent
        statement = self._revenue_statement(
            user, date_from, date_to, currency, campaign, source, referral_code
        )
        if group_by:
            period = func.date_trunc(
                group_by, event.occurred_at, "UTC", type_=DateTime(timezone=True)
            )
            statement = (
                statement.add_columns(period.label("period_start"))
                .group_by(
                    func.grouping_sets(tuple_(event.currency, period), tuple_(event.currency))
                )
                .order_by(period, event.currency)
            )
        else:
            statement = statement.group_by(event.currency).order_by(event.currency)
        return (await self.session.execute(statement)).all()

    async def top_referrers(
        self,
        user: User,
        date_from: datetime | None,
        date_to: datetime | None,
        currency: str | None,
        group_by: Grouping | None,
        campaign: str | None,
        source: str | None,
        referral_code: str | None,
        limit: int,
    ):
        event = OrderFinancialEvent
        statement = self._revenue_statement(
            user, date_from, date_to, currency, campaign, source, referral_code, by_referrer=True
        )
        keys = (event.currency, ReferralAttribution.referrer_id)
        if group_by:
            period = func.date_trunc(
                group_by, event.occurred_at, "UTC", type_=DateTime(timezone=True)
            )
            statement = statement.add_columns(period.label("period_start")).group_by(
                func.grouping_sets(tuple_(*keys, period), tuple_(*keys))
            )
        else:
            statement = statement.group_by(*keys)
        totals = statement.subquery()
        partitions = [totals.c.currency]
        if group_by:
            partitions.append(totals.c.period_start)
        position = func.row_number().over(
            partition_by=partitions, order_by=(totals.c.net_revenue.desc(), totals.c.referrer_id)
        )
        ranked = select(
            *partitions,
            totals.c.referrer_id,
            totals.c.paid_orders,
            totals.c.net_revenue,
            position.label("position"),
        ).subquery()
        sort = [ranked.c.currency, ranked.c.position]
        if group_by:
            sort.insert(0, ranked.c.period_start.nulls_first())
        return (
            (
                await self.session.execute(
                    select(ranked).where(ranked.c.position <= limit).order_by(*sort)
                )
            )
            .mappings()
            .all()
        )

    @staticmethod
    def _revenue_statement(
        user: User,
        date_from: datetime | None,
        date_to: datetime | None,
        currency: str | None,
        campaign: str | None,
        source: str | None,
        referral_code: str | None,
        *,
        by_referrer: bool = False,
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
                func.round(
                    func.avg(event.amount).filter(event.event_type == FinancialEventType.PAYMENT), 2
                ).label("average_order_value"),
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
        if by_referrer or campaign is not None or source is not None or referral_code is not None:
            statement = statement.join(
                ReferralAttribution, ReferralAttribution.id == Order.referral_attribution_id
            )
        if campaign is not None or source is not None:
            statement = statement.join(
                ReferralClick, ReferralClick.id == ReferralAttribution.click_id
            )
            if campaign is not None:
                statement = statement.where(ReferralClick.campaign == campaign)
            if source is not None:
                statement = statement.where(ReferralClick.source == source)
        if referral_code is not None:
            statement = statement.join(
                ReferralCode, ReferralCode.id == ReferralAttribution.referral_code_id
            ).where(ReferralCode.code == referral_code)
        if by_referrer:
            statement = statement.add_columns(ReferralAttribution.referrer_id)
        return statement
