import hashlib
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import FinancialEventType, Order, OrderFinancialEvent, OrderStatus
from app.modules.referrals.models import ReferralAttribution
from app.modules.rewards.service import RewardAmountOverflow, RewardService
from app.modules.tracking.models import Event
from app.modules.users.models import User


class DeferredOrderEvent(Exception):
    def __init__(self, code: str):
        self.code = code


class RejectedOrderEvent(Exception):
    def __init__(self, code: str):
        self.code = code


class OrderEventProcessor:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def process(self, event: Event) -> bool:
        """Return whether this event repeats an applied financial operation."""
        payload = event.payload
        external_id = payload["external_order_id"]
        digest = hashlib.blake2s(
            (event.source_system + "\0" + external_id).encode(), digest_size=4
        ).digest()
        lock_key = int.from_bytes(digest, byteorder="big", signed=True)
        await self.session.execute(select(func.pg_advisory_xact_lock(0x4F524452, lock_key)))
        order = await self.session.scalar(
            select(Order)
            .where(
                Order.source_system == event.source_system, Order.external_order_id == external_id
            )
            .with_for_update()
        )
        if event.event_type == "order_created":
            await self._create(event, order)
            return False
        if order is None:
            raise DeferredOrderEvent("order_missing")
        event.user_id = order.user_id
        if order.external_created_at is None:
            raise RejectedOrderEvent("order_creation_time_missing")
        if event.occurred_at < order.external_created_at:
            raise RejectedOrderEvent("event_before_order")
        if event.event_type == "order_cancelled":
            if order.status == OrderStatus.CANCELLED:
                return False
            if order.status != OrderStatus.CREATED:
                raise RejectedOrderEvent("cancel_after_payment")
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = event.occurred_at
            return False
        return await self._financial(event, order)

    async def _create(self, event: Event, order: Order | None) -> None:
        payload = event.payload
        from uuid import UUID

        user_id = UUID(payload["user_id"])
        if order is not None:
            if (
                order.user_id != user_id
                or order.total_amount != Decimal(payload["total_amount"])
                or order.currency != payload["currency"]
                or order.external_created_at != event.occurred_at
            ):
                raise RejectedOrderEvent("order_identity_conflict")
            event.user_id = order.user_id
            return
        user = await self.session.get(User, user_id)
        if user is None:
            raise DeferredOrderEvent("user_missing")
        if event.occurred_at < user.created_at:
            raise RejectedOrderEvent("order_before_registration")
        attribution = await self.session.scalar(
            select(ReferralAttribution).where(
                ReferralAttribution.referred_user_id == user_id,
                ReferralAttribution.attributed_at <= event.occurred_at,
            )
        )
        order = Order(
            source_system=event.source_system,
            external_order_id=payload["external_order_id"],
            user_id=user_id,
            total_amount=Decimal(payload["total_amount"]),
            currency=payload["currency"],
            status=OrderStatus.CREATED,
            external_created_at=event.occurred_at,
            referral_attribution_id=attribution.id if attribution else None,
            attribution_frozen_at=await self.session.scalar(select(func.clock_timestamp())),
        )
        self.session.add(order)
        event.user_id = user_id
        await self.session.flush()

    async def _financial(self, event: Event, order: Order) -> bool:
        payload = event.payload
        if event.event_type == "order_paid":
            kind = FinancialEventType.PAYMENT
        elif event.event_type == "order_refunded":
            kind = FinancialEventType.REFUND
        elif payload["direction"] == "credit":
            kind = FinancialEventType.ADJUSTMENT_CREDIT
        else:
            kind = FinancialEventType.ADJUSTMENT_DEBIT
        external_operation = payload["operation_id"]
        operation_id = f"{kind.value}:{external_operation}"
        aliases = [operation_id]
        if kind in (FinancialEventType.ADJUSTMENT_CREDIT, FinancialEventType.ADJUSTMENT_DEBIT):
            operation_id = f"adjustment:{external_operation}"
            aliases = [
                operation_id,
                f"adjustment_credit:{external_operation}",
                f"adjustment_debit:{external_operation}",
            ]
            # Serialize identity checks across orders, including legacy directional IDs.
            key = hashlib.blake2s(
                (event.source_system + "\0" + operation_id).encode(), digest_size=4
            ).digest()
            await self.session.execute(
                select(
                    func.pg_advisory_xact_lock(0x41444A55, int.from_bytes(key, "big", signed=True))
                )
            )
        amount = Decimal(payload["amount"])
        existing = (
            await self.session.scalars(
                select(OrderFinancialEvent).where(
                    OrderFinancialEvent.source_system == event.source_system,
                    OrderFinancialEvent.external_financial_event_id.in_(aliases),
                )
            )
        ).all()
        if existing:
            if any(
                row.order_id != order.id
                or row.event_type != kind
                or row.amount != amount
                or row.currency != payload["currency"]
                or row.occurred_at != event.occurred_at
                for row in existing
            ):
                raise RejectedOrderEvent("operation_identity_conflict")
            return True
        if order.currency != payload["currency"]:
            raise RejectedOrderEvent("currency_mismatch")
        if kind == FinancialEventType.PAYMENT:
            if order.status != OrderStatus.CREATED:
                raise RejectedOrderEvent("payment_not_allowed")
            if amount != order.total_amount:
                raise RejectedOrderEvent("payment_amount_mismatch")
        else:
            if order.status == OrderStatus.CREATED:
                raise DeferredOrderEvent("payment_missing")
            if order.status == OrderStatus.CANCELLED or order.paid_at is None:
                raise RejectedOrderEvent("financial_event_not_allowed")
            if event.occurred_at < order.paid_at:
                raise RejectedOrderEvent("financial_event_before_payment")

        operation = OrderFinancialEvent(
            order_id=order.id,
            source_system=event.source_system,
            external_financial_event_id=operation_id,
            event_type=kind,
            amount=amount,
            currency=order.currency,
            occurred_at=event.occurred_at,
        )
        self.session.add(operation)
        await self.session.flush()
        if kind == FinancialEventType.PAYMENT:
            order.status = OrderStatus.PAID
            order.paid_at = event.occurred_at
            await self._reward(order, operation)
            return False

        # Re-evaluate the complete chronological balance in PostgreSQL, including late events.
        financial = OrderFinancialEvent
        signed = case(
            (
                financial.event_type.in_(
                    [FinancialEventType.PAYMENT, FinancialEventType.ADJUSTMENT_CREDIT]
                ),
                financial.amount,
            ),
            else_=-financial.amount,
        )
        priority = case(
            (financial.event_type == FinancialEventType.PAYMENT, 0),
            (financial.event_type == FinancialEventType.ADJUSTMENT_CREDIT, 1),
            else_=2,
        )
        ledger = (
            select(
                signed.label("delta"),
                case(
                    (financial.event_type == FinancialEventType.REFUND, financial.amount), else_=0
                ).label("refund"),
                func.sum(signed)
                .over(order_by=(financial.occurred_at, priority, financial.id), rows=(None, 0))
                .label("running_balance"),
            )
            .where(financial.order_id == order.id)
            .subquery()
        )
        balance, refunded, minimum = (
            await self.session.execute(
                select(
                    func.sum(ledger.c.delta),
                    func.sum(ledger.c.refund),
                    func.min(ledger.c.running_balance),
                )
            )
        ).one()
        if minimum < 0:
            raise DeferredOrderEvent(
                "refund_exceeds_payment"
                if kind == FinancialEventType.REFUND
                else "adjustment_exceeds_balance"
            )
        if refunded > 0:
            order.status = OrderStatus.REFUNDED if balance == 0 else OrderStatus.PARTIALLY_REFUNDED
        else:
            order.status = OrderStatus.PAID

        await self._reward(order, operation)
        return False

    async def _reward(self, order: Order, operation: OrderFinancialEvent) -> None:
        try:
            await RewardService(self.session).reconcile(order, operation)
        except RewardAmountOverflow as exc:
            raise RejectedOrderEvent("reward_amount_overflow") from exc
