from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.modules.orders.models import Order, OrderFinancialEvent
from app.modules.referrals.models import ReferralAttribution, ReferralCode
from app.modules.users.models import UserRole
from tests.integration.test_overview import seed_overview
from tests.integration.test_referral_programs import staff_headers
from tests.integration.test_referral_statistics import stamp

URL = "/api/v1/analytics/top-referrers"


async def test_ranks_per_currency_and_period(client, session_maker):
    _, code = await seed_overview(client, session_maker)
    async with session_maker() as session:
        first = await session.scalar(select(ReferralCode.owner_id).where(ReferralCode.code == code))
        other = await session.scalar(
            select(ReferralAttribution).where(ReferralAttribution.referrer_id != first)
        )
        second = other.referrer_id
        for currency, amount in (("KZT", "22.01"), ("USD", "7.00")):
            order = Order(
                user_id=other.referred_user_id,
                currency=currency,
                total_amount=Decimal(amount),
                status="paid",
                paid_at=stamp(3),
                external_order_id=str(uuid4()),
                source_system="shop",
                referral_attribution_id=other.id,
                attribution_frozen_at=stamp(1),
            )
            session.add(order)
            await session.flush()
            operations = [("payment", amount, 3)]
            if currency == "KZT":
                operations.append(("adjustment_debit", "1.00", 4))
            for kind, value, day in operations:
                session.add(
                    OrderFinancialEvent(
                        order_id=order.id,
                        currency=currency,
                        source_system="shop",
                        external_financial_event_id=str(uuid4()),
                        event_type=kind,
                        amount=Decimal(value),
                        occurred_at=stamp(day),
                    )
                )
        await session.commit()
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    response = await client.get(URL, headers=admin)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    data = response.json()
    ids = sorted(map(str, (first, second)))
    assert data["periods"] == [] and data["limit"] == 10
    assert [
        (row["referrer_id"], row["currency"], row["position"], row["net_revenue"])
        for row in data["items"]
    ] == [(ids[0], "KZT", 1, "21.01"), (ids[1], "KZT", 2, "21.01"), (str(second), "USD", 1, "7.00")]
    assert all(
        set(row) == {"referrer_id", "currency", "position", "net_revenue", "paid_orders"}
        for row in data["items"]
    )
    revenue = (await client.get("/api/v1/analytics/revenue", headers=admin)).json()["items"]
    for item in revenue:
        assert sum(
            Decimal(row["net_revenue"])
            for row in data["items"]
            if row["currency"] == item["currency"]
        ) == Decimal(item["referral_attributed_revenue"])
    for group in ("day", "week", "month"):
        report = (
            await client.get(URL, headers=admin, params={"limit": 1, "group_by": group})
        ).json()
        assert [row["currency"] for row in report["items"]] == ["KZT", "USD"]
        assert report["items"][0]["referrer_id"] == ids[0]
        if group == "day":
            assert [p["period_start"] for p in report["periods"]] == [
                f"2026-01-0{d}T00:00:00Z" for d in (2, 3, 4, 5)
            ]
            refund = report["periods"][-1]["items"]
            assert (
                len(refund) == 1
                and refund[0]["net_revenue"] == "-2.00"
                and refund[0]["paid_orders"] == 0
            )
        else:
            assert all(row["position"] == 1 for p in report["periods"] for row in p["items"])


async def test_access_and_channel_filters(client, session_maker):
    customer, code = await seed_overview(client, session_maker)
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    for headers, status in (({}, 401), (customer, 403), (manager, 403)):
        assert (await client.get(URL, headers=headers)).status_code == status
    params = {
        "source": "mail",
        "campaign": "winter",
        "referral_code": code,
        "date_from": "2026-01-02T05:00:00+05:00",
        "date_to": stamp(5).isoformat(),
        "currency": "KZT",
        "group_by": "day",
    }
    data = (await client.get(URL, headers=admin, params=params)).json()
    assert data["date_from"] == "2026-01-02T00:00:00Z"
    assert len(data["items"]) == 1 and data["items"][0]["net_revenue"] == "23.01"
    assert data["items"][0]["paid_orders"] == 2
    for changes in (
        {"currency": "USD"},
        {"source": "other"},
        {"campaign": "other"},
        {"referral_code": "unknown"},
    ):
        empty = (await client.get(URL, headers=admin, params=params | changes)).json()
        assert empty["items"] == empty["periods"] == []
    for invalid in (
        {"limit": 0},
        {"limit": 101},
        {"currency": "kzt"},
        {"group_by": "year"},
        {"date_from": params["date_to"]},
        {"date_to": "2026-01-03T00:00:00"},
    ):
        assert (await client.get(URL, headers=admin, params=params | invalid)).status_code == 422
