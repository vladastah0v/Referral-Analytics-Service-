import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.orders.models import Order, OrderFinancialEvent, OrderStatus
from app.modules.tracking.models import Event, EventOutbox
from app.modules.tracking.outbox import process_next
from app.modules.tracking.security import create_credential
from app.modules.users.models import UserRole
from tests.integration.test_auth import bearer, login
from tests.integration.test_lead_creation import own_user_id
from tests.integration.test_referral_attribution import click, make_code, register_with_visitor
from tests.integration.test_referral_codes import authenticated
from tests.integration.test_referral_programs import staff_headers

EVENTS = "/api/v1/events"


async def service_key(
    factory: async_sessionmaker[AsyncSession], source: str = "shop"
) -> dict[str, str]:
    async with factory() as session:
        _, key = await create_credential(session, source)
        return {"X-Integration-Key": key}


def event(
    event_type: str, payload: dict, when: datetime | None = None, event_id: str | None = None
):
    return {
        "external_event_id": event_id or str(uuid4()),
        "event_type": event_type,
        "occurred_at": (when or datetime.now(UTC)).isoformat(),
        "payload": payload,
    }


async def process_one(factory: async_sessionmaker[AsyncSession]) -> bool:
    async with factory() as session:
        return await process_next(session)


async def make_user(client: AsyncClient, email: str = "buyer@example.com"):
    headers = await authenticated(client, email)
    return headers, await own_user_id(client, headers)


def created_payload(user_id, external_id="order-1", currency="KZT", amount="100.00"):
    return {
        "external_order_id": external_id,
        "user_id": str(user_id),
        "total_amount": amount,
        "currency": currency,
    }


def money_payload(op, external_id="order-1", currency="KZT", amount="100.00"):
    return {
        "external_order_id": external_id,
        "operation_id": op,
        "amount": amount,
        "currency": currency,
    }


async def test_integration_auth_batch_idempotency_and_outbox(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    customer, user_id = await make_user(client)
    headers = await service_key(session_maker)
    payload = event("order_created", created_payload(user_id))
    assert (await client.post(EVENTS, headers=customer, json=payload)).status_code == 401
    assert (
        await client.post(EVENTS, headers={"X-Integration-Key": "invalid"}, json=payload)
    ).status_code == 401
    first = await client.post(EVENTS, headers=headers, json=payload)
    assert first.status_code == 202
    replay = await client.post(EVENTS, headers=headers, json=payload)
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    changed = payload | {"payload": created_payload(user_id, amount="99.00")}
    assert (await client.post(EVENTS, headers=headers, json=changed)).status_code == 409
    bad = event("order_paid", money_payload("x") | {"amount": 1.0})
    another = event("order_paid", money_payload("p"))
    batch = await client.post(
        EVENTS + "/batch", headers=headers, json={"events": [payload, changed, bad, another, None]}
    )
    assert batch.status_code == 207
    assert [item["status_code"] for item in batch.json()["results"]] == [200, 409, 422, 202, 422]
    assert (
        await client.post(EVENTS + "/batch", headers=headers, json={"events": [payload] * 101})
    ).status_code == 422
    assert (
        await client.post(EVENTS, headers=headers, json=payload | {"source_system": "forged"})
    ).status_code == 422
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 2
        assert await session.scalar(select(func.count()).select_from(EventOutbox)) == 2


async def test_paid_partial_full_refunds_and_revenue(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, code = await make_code(client, "referrer@example.com")
    visitor = uuid4()
    await click(client, code["code"], visitor)
    registered = await register_with_visitor(client, "buyer@example.com", visitor)
    buyer_id = UUID(registered.json()["id"])
    buyer = bearer((await login(client, "buyer@example.com")).json()["access_token"])
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    messages = [
        event("order_created", created_payload(buyer_id), now),
        event("order_paid", money_payload("payment-1"), now + timedelta(seconds=1)),
        event(
            "order_refunded", money_payload("refund-1", amount="30.00"), now + timedelta(seconds=2)
        ),
        event(
            "order_refunded", money_payload("refund-2", amount="70.00"), now + timedelta(seconds=3)
        ),
    ]
    for message in messages:
        assert (await client.post(EVENTS, headers=headers, json=message)).status_code == 202
        assert await process_one(session_maker)
    duplicate_operation = event(
        "order_paid", money_payload("payment-1"), now + timedelta(seconds=1)
    )
    assert (await client.post(EVENTS, headers=headers, json=duplicate_operation)).status_code == 202
    assert await process_one(session_maker)
    async with session_maker() as session:
        order = await session.scalar(select(Order))
        assert order.status == OrderStatus.REFUNDED
        assert order.referral_attribution_id is not None and order.attribution_frozen_at is not None
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 3
        assert (
            await session.scalar(
                select(func.count()).select_from(EventOutbox).where(EventOutbox.state == "done")
            )
            == 5
        )
    result = await client.get("/api/v1/orders", headers=buyer)
    assert result.status_code == 200 and len(result.json()["items"]) == 1
    assert result.json()["items"][0]["status"] == "refunded"
    assert (await client.get("/api/v1/analytics/revenue", headers=buyer)).status_code == 403
    revenue = await client.get("/api/v1/analytics/revenue", headers=admin)
    assert revenue.status_code == 200
    row = revenue.json()["items"][0]
    assert row == {
        "currency": "KZT",
        "paid_orders": 1,
        "gross_revenue": "100.00",
        "refunds": "100.00",
        "adjustments_debit": "0.00",
        "net_revenue": "0.00",
        "referral_attributed_revenue": "0.00",
    }
    payment_window = await client.get(
        "/api/v1/analytics/revenue",
        headers=admin,
        params={
            "date_from": (now + timedelta(seconds=1)).isoformat(),
            "date_to": (now + timedelta(seconds=2)).isoformat(),
        },
    )
    payment_row = payment_window.json()["items"][0]
    assert payment_row["paid_orders"] == 1 and payment_row["net_revenue"] == "100.00"
    refund_window = await client.get(
        "/api/v1/analytics/revenue",
        headers=admin,
        params={
            "date_from": (now + timedelta(seconds=2)).isoformat(),
            "date_to": (now + timedelta(seconds=3)).isoformat(),
        },
    )
    refund_row = refund_window.json()["items"][0]
    assert refund_row["paid_orders"] == 0 and refund_row["net_revenue"] == "-30.00"


async def test_out_of_order_events_are_retried(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    refund = event("order_refunded", money_payload("r", amount="20.00"), now + timedelta(seconds=2))
    paid = event("order_paid", money_payload("p"), now + timedelta(seconds=1))
    created = event("order_created", created_payload(user_id), now)

    async def run_event(receipt_id: str) -> None:
        async with session_maker() as session:
            await session.execute(
                update(EventOutbox)
                .where(EventOutbox.state == "pending")
                .values(available_at=datetime.now(UTC) + timedelta(hours=1))
            )
            await session.execute(
                update(EventOutbox)
                .where(EventOutbox.event_id == UUID(receipt_id))
                .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        assert await process_one(session_maker)

    refund_response = await client.post(EVENTS, headers=headers, json=refund)
    assert refund_response.status_code == 202
    await run_event(refund_response.json()["id"])
    async with session_maker() as session:
        row = await session.scalar(select(EventOutbox))
        assert row.state == "pending" and row.last_error == "order_missing"
    paid_response = await client.post(EVENTS, headers=headers, json=paid)
    assert paid_response.status_code == 202
    await run_event(paid_response.json()["id"])
    created_response = await client.post(EVENTS, headers=headers, json=created)
    assert created_response.status_code == 202
    await run_event(created_response.json()["id"])
    await run_event(paid_response.json()["id"])
    await run_event(refund_response.json()["id"])
    async with session_maker() as session:
        order = await session.scalar(select(Order))
        assert order.status == OrderStatus.PARTIALLY_REFUNDED
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 2


async def test_parallel_refunds_cannot_exceed_payment(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    for message in (
        event("order_created", created_payload(user_id), now),
        event("order_paid", money_payload("p"), now + timedelta(seconds=1)),
    ):
        assert (await client.post(EVENTS, headers=headers, json=message)).status_code == 202
        assert await process_one(session_maker)
    for op in ("r1", "r2"):
        assert (
            await client.post(
                EVENTS,
                headers=headers,
                json=event(
                    "order_refunded", money_payload(op, amount="70.00"), now + timedelta(seconds=2)
                ),
            )
        ).status_code == 202
    assert all(await asyncio.gather(process_one(session_maker), process_one(session_maker)))
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(
                    EventOutbox.last_error == "refund_exceeds_payment",
                    EventOutbox.state == "pending",
                )
            )
            == 1
        )
        assert (await session.scalar(select(Order))).status == OrderStatus.PARTIALLY_REFUNDED


async def test_order_and_revenue_access_currency_separation(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    first, first_id = await make_user(client, "first@example.com")
    second, second_id = await make_user(client, "second@example.com")
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    manager_id = await own_user_id(client, manager)
    lead = (await client.post("/api/v1/leads", headers=first, json={"source": "site"})).json()
    assert (
        await client.patch(
            f"/api/v1/leads/{lead['id']}/assign",
            headers=admin,
            json={"manager_id": str(manager_id)},
        )
    ).status_code == 200
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    receipts = []
    for user_id, external_id, currency, amount in (
        (first_id, "first-order", "KZT", "100.00"),
        (second_id, "second-order", "USD", "25.00"),
    ):
        for message in (
            event("order_created", created_payload(user_id, external_id, currency, amount), now),
            event(
                "order_paid",
                money_payload(f"pay-{external_id}", external_id, currency, amount),
                now + timedelta(seconds=1),
            ),
        ):
            receipt = await client.post(EVENTS, headers=headers, json=message)
            assert receipt.status_code == 202
            receipts.append(receipt.json()["id"])
            assert await process_one(session_maker)
    own = (await client.get("/api/v1/orders", headers=first)).json()["items"]
    other = (await client.get("/api/v1/orders", headers=second)).json()["items"]
    assert len(own) == len(other) == 1
    assert (await client.get(f"/api/v1/orders/{other[0]['id']}", headers=first)).status_code == 404
    assert (await client.get(f"/api/v1/orders/{own[0]['id']}", headers=manager)).status_code == 200
    assert (
        await client.get(f"/api/v1/orders/{other[0]['id']}", headers=manager)
    ).status_code == 404
    assert (await client.get(f"{EVENTS}/{receipts[0]}", headers=manager)).status_code == 200
    assert (await client.get(f"{EVENTS}/{receipts[2]}", headers=manager)).status_code == 404
    assert (await client.get(f"{EVENTS}/{receipts[2]}", headers=admin)).status_code == 200
    assert (await client.get(f"{EVENTS}/{receipts[0]}", headers=first)).status_code == 403
    manager_revenue = (await client.get("/api/v1/analytics/revenue", headers=manager)).json()[
        "items"
    ]
    admin_revenue = (await client.get("/api/v1/analytics/revenue", headers=admin)).json()["items"]
    assert [row["currency"] for row in manager_revenue] == ["KZT"]
    assert [row["currency"] for row in admin_revenue] == ["KZT", "USD"]
    assert [row["net_revenue"] for row in admin_revenue] == ["100.00", "25.00"]
    assert (
        await client.get(
            "/api/v1/analytics/revenue",
            headers=admin,
            params={"date_from": now.isoformat(), "date_to": now.isoformat()},
        )
    ).status_code == 422


async def test_cancellation_and_operation_replay_after_refund(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    created = event("order_created", created_payload(user_id), now)
    cancelled = event(
        "order_cancelled", {"external_order_id": "order-1"}, now + timedelta(seconds=1)
    )
    paid = event("order_paid", money_payload("p"), now + timedelta(seconds=2))
    for message in (created, cancelled, paid):
        assert (await client.post(EVENTS, headers=headers, json=message)).status_code == 202
        assert await process_one(session_maker)
    async with session_maker() as session:
        order = await session.scalar(select(Order))
        assert order.status == OrderStatus.CANCELLED
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.error_code == "payment_not_allowed")
            )
            == 1
        )


async def test_reprocessing_outbox_has_no_duplicate_effects(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    for message in (
        event("order_created", created_payload(user_id), now),
        event("order_paid", money_payload("p"), now + timedelta(seconds=1)),
    ):
        assert (await client.post(EVENTS, headers=headers, json=message)).status_code == 202
        assert await process_one(session_maker)
    async with session_maker() as session:
        await session.execute(
            update(EventOutbox).values(
                state="pending", available_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await session.commit()
    assert await process_one(session_maker) and await process_one(session_maker)
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 1
        assert (await session.scalar(select(Order))).status == OrderStatus.PAID


async def test_adjustments_are_idempotent_and_cannot_make_negative_balance(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, user_id = await make_user(client)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    messages = [
        event("order_created", created_payload(user_id), now),
        event("order_paid", money_payload("payment"), now + timedelta(seconds=1)),
        event(
            "order_adjusted",
            money_payload("credit", amount="10.00") | {"direction": "credit"},
            now + timedelta(seconds=2),
        ),
        event(
            "order_adjusted",
            money_payload("debit", amount="15.00") | {"direction": "debit"},
            now + timedelta(seconds=3),
        ),
        event(
            "order_adjusted",
            money_payload("excess", amount="100.00") | {"direction": "debit"},
            now + timedelta(seconds=4),
        ),
    ]
    for message in messages:
        assert (await client.post(EVENTS, headers=headers, json=message)).status_code == 202
        assert await process_one(session_maker)
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 3
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(
                    EventOutbox.last_error == "adjustment_exceeds_balance",
                    EventOutbox.state == "pending",
                )
            )
            == 1
        )
    row = (await client.get("/api/v1/analytics/revenue", headers=admin)).json()["items"][0]
    assert row["gross_revenue"] == "110.00"
    assert row["adjustments_debit"] == "15.00" and row["net_revenue"] == "95.00"


async def test_attribution_is_frozen_even_if_registration_attributed_later(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from app.modules.referrals.service import ReferralAttributionService

    _, code = await make_code(client, "referrer@example.com")
    visitor = uuid4()
    await click(client, code["code"], visitor)
    registered = await register_with_visitor(client, "buyer@example.com", None)
    user_id = UUID(registered.json()["id"])
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    assert (
        await client.post(
            EVENTS, headers=headers, json=event("order_created", created_payload(user_id), now)
        )
    ).status_code == 202
    assert await process_one(session_maker)
    async with session_maker() as session:
        order = await session.scalar(select(Order))
        assert order.referral_attribution_id is None and order.attribution_frozen_at is not None
        attribution = await ReferralAttributionService(session).attribute_registration(
            user_id, visitor
        )
        assert attribution is not None
        await session.commit()
    later = event("order_paid", money_payload("payment"), now + timedelta(seconds=1))
    assert (await client.post(EVENTS, headers=headers, json=later)).status_code == 202
    assert await process_one(session_maker)
    async with session_maker() as session:
        order = await session.scalar(select(Order))
        assert order.status == OrderStatus.PAID and order.referral_attribution_id is None


async def test_invalid_financial_transitions_are_rejected_without_side_effects(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    initial = event("order_created", created_payload(user_id), now)
    assert (await client.post(EVENTS, headers=headers, json=initial)).status_code == 202
    assert await process_one(session_maker)
    cases = [
        (
            event("order_paid", money_payload("wrong", amount="99.00"), now + timedelta(seconds=1)),
            "payment_amount_mismatch",
        ),
        (
            event(
                "order_paid",
                money_payload("wrong-currency", currency="USD"),
                now + timedelta(seconds=2),
            ),
            "currency_mismatch",
        ),
        (event("order_paid", money_payload("correct"), now + timedelta(seconds=5)), None),
        (
            event(
                "order_refunded", money_payload("early", amount="10.00"), now + timedelta(seconds=4)
            ),
            "financial_event_before_payment",
        ),
        (
            event("order_cancelled", {"external_order_id": "order-1"}, now + timedelta(seconds=6)),
            "cancel_after_payment",
        ),
        (
            event("order_paid", money_payload("second"), now + timedelta(seconds=7)),
            "payment_not_allowed",
        ),
    ]
    for message, error in cases:
        receipt = await client.post(EVENTS, headers=headers, json=message)
        assert receipt.status_code == 202
        assert await process_one(session_maker)
        async with session_maker() as session:
            stored = await session.get(Event, UUID(receipt.json()["id"]))
            assert stored.error_code == error
            assert stored.processing_status == ("rejected" if error else "processed")
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 1
        assert (await session.scalar(select(Order))).status == OrderStatus.PAID
    zero = event("order_created", created_payload(user_id, "zero", amount="0.00"))
    assert (await client.post(EVENTS, headers=headers, json=zero)).status_code == 422


async def test_service_key_source_isolation_and_revocation(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from app.modules.tracking.security import disable_credential

    _, user_id = await make_user(client)
    async with session_maker() as session:
        credential, first_key = await create_credential(session, "shop-a")
        credential_id = credential.id
        _, second_key = await create_credential(session, "shop-b")
    message = event("order_created", created_payload(user_id), event_id="same-external-event")
    assert (
        await client.post(EVENTS, headers={"X-Integration-Key": first_key}, json=message)
    ).status_code == 202
    assert (
        await client.post(EVENTS, headers={"X-Integration-Key": second_key}, json=message)
    ).status_code == 202
    async with session_maker() as session:
        assert await disable_credential(session, credential_id)
    assert (
        await client.post(EVENTS, headers={"X-Integration-Key": first_key}, json=message)
    ).status_code == 401
    assert (
        await client.post(EVENTS, headers={"X-Integration-Key": second_key}, json=message)
    ).status_code == 200


async def test_failed_prerequisite_can_be_requeued(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from app.modules.tracking.outbox import MAX_ATTEMPTS, requeue_failed_event

    _, user_id = await make_user(client)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    paid = event("order_paid", money_payload("payment"), now + timedelta(seconds=1))
    receipt = await client.post(EVENTS, headers=headers, json=paid)
    event_id = UUID(receipt.json()["id"])
    async with session_maker() as session:
        await session.execute(
            update(EventOutbox)
            .where(EventOutbox.event_id == event_id)
            .values(attempts=MAX_ATTEMPTS - 1)
        )
        await session.commit()
    assert await process_one(session_maker)
    detail = (await client.get(f"{EVENTS}/{event_id}", headers=admin)).json()
    assert detail["attempts"] == MAX_ATTEMPTS and detail["last_error"] == "order_missing"
    async with session_maker() as session:
        stored = await session.get(Event, event_id)
        assert (
            stored.processing_status == "rejected" and stored.error_code == "prerequisite_timeout"
        )
        assert await requeue_failed_event(session, event_id)
    created = event("order_created", created_payload(user_id), now)
    assert (await client.post(EVENTS, headers=headers, json=created)).status_code == 202
    async with session_maker() as session:
        await session.execute(
            update(EventOutbox)
            .where(EventOutbox.event_id == event_id)
            .values(available_at=datetime.now(UTC) + timedelta(hours=1))
        )
        await session.commit()
    assert await process_one(session_maker)
    async with session_maker() as session:
        await session.execute(
            update(EventOutbox)
            .where(EventOutbox.event_id == event_id)
            .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    assert await process_one(session_maker)
    async with session_maker() as session:
        assert (await session.get(Event, event_id)).processing_status == "processed"
        assert (await session.scalar(select(Order))).status == OrderStatus.PAID


async def test_concurrent_ingestion_is_idempotent(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    payload = event("order_created", created_payload(user_id))
    results = await asyncio.gather(
        *(client.post(EVENTS, headers=headers, json=payload) for _ in range(6))
    )
    assert sorted(response.status_code for response in results) == [200] * 5 + [202]
    assert len({response.json()["id"] for response in results}) == 1
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        assert await session.scalar(select(func.count()).select_from(EventOutbox)) == 1


async def test_parallel_messages_for_one_payment_do_not_duplicate_ledger(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    assert (
        await client.post(
            EVENTS, headers=headers, json=event("order_created", created_payload(user_id), now)
        )
    ).status_code == 202
    assert await process_one(session_maker)
    payments = [
        event("order_paid", money_payload("same-operation"), now + timedelta(seconds=1))
        for _ in range(2)
    ]
    for payment in payments:
        assert (await client.post(EVENTS, headers=headers, json=payment)).status_code == 202
    assert all(await asyncio.gather(process_one(session_maker), process_one(session_maker)))
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(Event).where(Event.processing_status == "rejected")
            )
            == 0
        )
        assert (await session.scalar(select(Order))).status == OrderStatus.PAID


async def test_refund_waits_for_earlier_adjustment_and_uses_adjusted_balance(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    for message in (
        event("order_created", created_payload(user_id), now),
        event("order_paid", money_payload("payment"), now + timedelta(seconds=1)),
    ):
        assert (await client.post(EVENTS, headers=headers, json=message)).status_code == 202
        assert await process_one(session_maker)
    refund = event(
        "order_refunded", money_payload("refund", amount="110.00"), now + timedelta(seconds=3)
    )
    receipt = await client.post(EVENTS, headers=headers, json=refund)
    refund_id = UUID(receipt.json()["id"])
    assert await process_one(session_maker)
    async with session_maker() as session:
        item = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == refund_id))
        assert item.state == "pending" and item.last_error == "refund_exceeds_payment"
    adjustment = event(
        "order_adjusted",
        money_payload("credit", amount="10.00") | {"direction": "credit"},
        now + timedelta(seconds=2),
    )
    assert (await client.post(EVENTS, headers=headers, json=adjustment)).status_code == 202
    async with session_maker() as session:
        await session.execute(
            update(EventOutbox)
            .where(EventOutbox.event_id == refund_id)
            .values(available_at=datetime.now(UTC) + timedelta(hours=1))
        )
        await session.commit()
    assert await process_one(session_maker)
    async with session_maker() as session:
        await session.execute(
            update(EventOutbox)
            .where(EventOutbox.event_id == refund_id)
            .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    assert await process_one(session_maker)
    async with session_maker() as session:
        assert (await session.scalar(select(Order))).status == OrderStatus.REFUNDED
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 3
        assert (await session.get(Event, refund_id)).processing_status == "processed"


async def test_ingestion_rolls_back_when_outbox_insert_fails(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.modules.tracking import service

    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    message = event("order_created", created_payload(user_id))
    original = service.EventOutbox
    monkeypatch.setattr(service, "EventOutbox", lambda **kwargs: EventOutbox(event_id=uuid4()))
    with pytest.raises(IntegrityError):
        await client.post(EVENTS, headers=headers, json=message)
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 0
        assert await session.scalar(select(func.count()).select_from(EventOutbox)) == 0
    monkeypatch.setattr(service, "EventOutbox", original)
    assert (await client.post(EVENTS, headers=headers, json=message)).status_code == 202


async def test_worker_failure_rolls_back_financial_effect_and_can_retry(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    from app.modules.orders.processor import OrderEventProcessor

    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    assert (
        await client.post(
            EVENTS, headers=headers, json=event("order_created", created_payload(user_id), now)
        )
    ).status_code == 202
    assert await process_one(session_maker)
    paid = await client.post(
        EVENTS,
        headers=headers,
        json=event("order_paid", money_payload("p"), now + timedelta(seconds=1)),
    )
    paid_id = UUID(paid.json()["id"])
    original = OrderEventProcessor._financial

    async def fail_after_flush(self, received_event, order):
        await original(self, received_event, order)
        raise RuntimeError("simulated worker failure")

    monkeypatch.setattr(OrderEventProcessor, "_financial", fail_after_flush)
    with pytest.raises(RuntimeError, match="simulated worker failure"):
        await process_one(session_maker)
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 0
        assert (await session.scalar(select(Order))).status == OrderStatus.CREATED
        assert (await session.get(Event, paid_id)).processing_status == "pending"
        assert (
            await session.scalar(select(EventOutbox).where(EventOutbox.event_id == paid_id))
        ).state == "pending"
    monkeypatch.setattr(OrderEventProcessor, "_financial", original)
    assert await process_one(session_maker)
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 1
        assert (await session.scalar(select(Order))).status == OrderStatus.PAID


async def test_same_operation_on_different_orders_never_credits_both(
    client: AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from sqlalchemy.exc import IntegrityError

    _, user_id = await make_user(client)
    headers = await service_key(session_maker)
    now = datetime.now(UTC)
    for external_id in ("one", "two"):
        response = await client.post(
            EVENTS,
            headers=headers,
            json=event("order_created", created_payload(user_id, external_id), now),
        )
        assert response.status_code == 202 and await process_one(session_maker)
    for external_id in ("one", "two"):
        response = await client.post(
            EVENTS,
            headers=headers,
            json=event(
                "order_paid",
                money_payload("same-operation", external_id),
                now + timedelta(seconds=1),
            ),
        )
        assert response.status_code == 202
    results = await asyncio.gather(
        process_one(session_maker), process_one(session_maker), return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception):
            assert isinstance(result, IntegrityError)
            assert await process_one(session_maker)
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(OrderFinancialEvent)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(Order).where(Order.status == OrderStatus.PAID)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.error_code == "operation_identity_conflict")
            )
            == 1
        )
