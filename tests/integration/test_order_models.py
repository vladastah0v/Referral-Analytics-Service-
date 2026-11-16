from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.orders.models import FinancialEventType, Order, OrderFinancialEvent, OrderStatus
from app.modules.referrals.models import (
    AttributionModel,
    ReferralAttribution,
    ReferralClick,
    ReferralCode,
)
from tests.integration.test_lead_models import add_user, rejected


async def test_order_constraints_and_source_scope(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        user_id = await add_user(connection)
        order_id = uuid4()
        base = {
            "user_id": user_id,
            "source_system": "shop",
            "external_order_id": "order-1",
            "total_amount": Decimal("1234.56"),
            "currency": "KZT",
        }
        await connection.execute(insert(Order).values(id=order_id, **base))
        row = (await connection.execute(select(Order).where(Order.id == order_id))).one()
        assert row.status == OrderStatus.CREATED
        assert row.total_amount == Decimal("1234.56") and isinstance(row.total_amount, Decimal)
        assert row.paid_at is None and row.created_at.tzinfo is not None
        await rejected(connection, insert(Order).values(id=uuid4(), **base))
        await connection.execute(
            insert(Order).values(id=uuid4(), **(base | {"source_system": "another"}))
        )
        for changes in (
            {"total_amount": Decimal("-1")},
            {"total_amount": Decimal("NaN")},
            {"currency": "kzt"},
            {"source_system": "  "},
            {"external_order_id": "  "},
            {"status": OrderStatus.PAID},
            {"status": OrderStatus.PARTIALLY_REFUNDED},
        ):
            values = base | {"external_order_id": str(uuid4())} | changes
            await rejected(connection, insert(Order).values(id=uuid4(), **values))
        await connection.execute(
            insert(Order).values(
                id=uuid4(),
                **(
                    base
                    | {
                        "external_order_id": "paid-order",
                        "status": OrderStatus.PAID,
                        "paid_at": datetime.now(UTC),
                    }
                ),
            )
        )
        invalid = text(
            "INSERT INTO orders (id,user_id,source_system,external_order_id,"
            "total_amount,currency,status) "
            "VALUES (:id,:user_id,'shop','bad-status',1,'KZT','unknown')"
        ).bindparams(id=uuid4(), user_id=user_id)
        await rejected(connection, invalid)
        await transaction.rollback()


async def test_financial_events_currency_idempotency_and_history(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        user_id = await add_user(connection)
        order_id = uuid4()
        await connection.execute(
            insert(Order).values(
                id=order_id,
                user_id=user_id,
                source_system="shop",
                external_order_id="one",
                total_amount=Decimal("100.00"),
                currency="KZT",
            )
        )
        base = {
            "order_id": order_id,
            "source_system": "gateway",
            "external_financial_event_id": "operation-1",
            "event_type": FinancialEventType.PAYMENT,
            "amount": Decimal("100.00"),
            "currency": "KZT",
            "occurred_at": datetime.now(UTC),
        }
        event_id = uuid4()
        await connection.execute(insert(OrderFinancialEvent).values(id=event_id, **base))
        await rejected(connection, insert(OrderFinancialEvent).values(id=uuid4(), **base))
        await connection.execute(
            insert(OrderFinancialEvent).values(
                id=uuid4(),
                **(
                    base
                    | {
                        "source_system": "another-gateway",
                        "event_type": FinancialEventType.REFUND,
                        "amount": Decimal("10.00"),
                    }
                ),
            )
        )
        for changes in (
            {"amount": Decimal("0")},
            {"amount": Decimal("-1")},
            {"amount": Decimal("NaN")},
            {"currency": "USD"},
            {"currency": "kzt"},
            {"source_system": " "},
            {"external_financial_event_id": "  "},
            {"order_id": uuid4()},
        ):
            values = base | {"external_financial_event_id": str(uuid4())} | changes
            await rejected(connection, insert(OrderFinancialEvent).values(id=uuid4(), **values))
        stored = (
            await connection.execute(
                select(OrderFinancialEvent).where(OrderFinancialEvent.id == event_id)
            )
        ).one()
        assert stored.amount == Decimal("100.00") and stored.recorded_at.tzinfo is not None
        await rejected(connection, delete(Order).where(Order.id == order_id))
        other_order = uuid4()
        await connection.execute(
            insert(Order).values(
                id=other_order,
                user_id=user_id,
                source_system="shop",
                external_order_id="two",
                total_amount=Decimal("100.00"),
                currency="KZT",
            )
        )
        await rejected(
            connection,
            insert(OrderFinancialEvent).values(id=uuid4(), **(base | {"order_id": other_order})),
        )
        for kind in (FinancialEventType.ADJUSTMENT_CREDIT, FinancialEventType.ADJUSTMENT_DEBIT):
            await connection.execute(
                insert(OrderFinancialEvent).values(
                    id=uuid4(),
                    **(base | {"event_type": kind, "external_financial_event_id": str(uuid4())}),
                )
            )
        await transaction.rollback()


async def test_order_attribution_must_match_customer(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        referrer, referred, other = [await add_user(connection) for _ in range(3)]
        code_id, click_id, attribution_id = uuid4(), uuid4(), uuid4()
        await connection.execute(
            insert(ReferralCode).values(id=code_id, owner_id=referrer, code="orders-ref")
        )
        await connection.execute(
            insert(ReferralClick).values(
                id=click_id, referral_code_id=code_id, visitor_id=uuid4(), request_id=uuid4()
            )
        )
        await connection.execute(
            insert(ReferralAttribution).values(
                id=attribution_id,
                referral_code_id=code_id,
                referrer_id=referrer,
                referred_user_id=referred,
                click_id=click_id,
                attribution_model=AttributionModel.LAST_REFERRAL_CLICK,
            )
        )
        base = {"source_system": "shop", "total_amount": Decimal("100.00"), "currency": "KZT"}
        await rejected(
            connection,
            insert(Order).values(
                id=uuid4(),
                user_id=referred,
                referral_attribution_id=attribution_id,
                external_order_id="not-frozen",
                **base,
            ),
        )
        await connection.execute(
            insert(Order).values(
                id=uuid4(),
                user_id=referred,
                referral_attribution_id=attribution_id,
                attribution_frozen_at=datetime.now(UTC),
                external_order_id="correct",
                **base,
            )
        )
        await rejected(
            connection,
            insert(Order).values(
                id=uuid4(),
                user_id=other,
                referral_attribution_id=attribution_id,
                attribution_frozen_at=datetime.now(UTC),
                external_order_id="wrong",
                **base,
            ),
        )
        direct_id = uuid4()
        await connection.execute(
            insert(Order).values(
                id=direct_id,
                user_id=other,
                referral_attribution_id=None,
                attribution_frozen_at=datetime.now(UTC),
                external_order_id="direct",
                **base,
            )
        )
        direct = (await connection.execute(select(Order).where(Order.id == direct_id))).one()
        assert direct.referral_attribution_id is None and direct.attribution_frozen_at is not None
        await rejected(
            connection, delete(ReferralAttribution).where(ReferralAttribution.id == attribution_id)
        )
        await transaction.rollback()
