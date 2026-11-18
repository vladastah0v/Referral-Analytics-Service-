from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, update

from app.modules.analytics import overview
from app.modules.leads.models import Lead
from app.modules.orders.models import Order, OrderFinancialEvent
from app.modules.referrals.models import ReferralAttribution, ReferralClick, ReferralCode
from app.modules.users.models import UserRole
from tests.integration.test_referral_programs import staff_headers
from tests.integration.test_referral_statistics import seed_statistics, stamp

URL = "/api/v1/analytics/overview"


async def seed_overview(client, factory):
    customer, _, _ = await seed_statistics(client, factory)
    async with factory() as session:
        attr, code = (
            await session.execute(
                select(ReferralAttribution, ReferralCode.code)
                .join(ReferralCode, ReferralCode.id == ReferralAttribution.referral_code_id)
                .order_by(ReferralAttribution.attributed_at)
                .limit(1)
            )
        ).one()
        await session.execute(
            update(ReferralClick)
            .where(ReferralClick.id == attr.click_id)
            .values(source="mail", campaign="winter")
        )
        session.add(
            Lead(
                user_id=attr.referred_user_id,
                referral_attribution_id=attr.id,
                source="mail",
                created_at=stamp(1),
                qualified_at=stamp(2),
            )
        )
        for currency, amount, attributed in [
            ("KZT", "10.00", True),
            ("KZT", "10.01", True),
            ("USD", "50.00", False),
        ]:
            order = Order(
                user_id=attr.referred_user_id,
                currency=currency,
                total_amount=Decimal(amount),
                status="paid",
                paid_at=stamp(2),
                external_order_id=str(uuid4()),
                source_system="shop",
                referral_attribution_id=attr.id if attributed else None,
                attribution_frozen_at=stamp(1),
            )
            session.add(order)
            await session.flush()
            ops = [("payment", amount, 2)]
            if amount == "10.00":
                ops += [("adjustment_credit", "3.00", 2), ("refund", "2.00", 5)]
            for kind, value, day in ops:
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
    return customer, code


async def test_overview_matches_reports_and_separates_currencies(client, session_maker):
    customer, code = await seed_overview(client, session_maker)
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    for headers, expected in (({}, 401), (customer, 403), (manager, 403)):
        assert (await client.get(URL, headers=headers)).status_code == expected
    params = {"date_from": "2026-01-01T05:00:00+05:00", "date_to": stamp(5).isoformat()}
    response = await client.get(URL, headers=admin, params=params)
    assert response.status_code == 200, response.text
    data = response.json()
    assert response.headers["cache-control"] == "private, no-store"
    assert data["periods"] == [] and data["date_from"] == "2026-01-01T00:00:00Z"
    for section in ("referrals", "funnel", "revenue"):
        standalone = (
            await client.get("/api/v1/analytics/" + section, headers=admin, params=params)
        ).json()
        if section == "revenue":
            assert [
                dict((k, v) for k, v in item.items() if k != "average_order_value")
                for item in data[section]
            ] == standalone["items"]
        else:
            assert data[section] == {
                k: v
                for k, v in standalone.items()
                if k not in ("periods", "date_from", "date_to", "group_by")
            }
    assert [
        (r["currency"], r["paid_orders"], r["gross_revenue"], r["average_order_value"])
        for r in data["revenue"]
    ] == [("KZT", 2, "23.01", "10.01"), ("USD", 1, "50.00", "50.00")]
    filters = {"campaign": "winter", "source": "mail", "referral_code": code, "group_by": "day"}
    grouped = (await client.get(URL, headers=admin, params=filters)).json()
    assert grouped["referrals"]["unique_visitors"] == grouped["funnel"]["total_leads"] == 1
    assert [row["currency"] for row in grouped["revenue"]] == ["KZT"]
    assert [b["period_start"] for b in grouped["periods"]] == [
        f"2026-01-0{d}T00:00:00Z" for d in (1, 2, 5)
    ]
    last = grouped["periods"][-1]
    assert last["referrals"]["conversion_rate"] is None and last["funnel"]["total_leads"] == 0
    assert last["revenue"][0]["net_revenue"] == "-2.00"
    assert last["revenue"][0]["average_order_value"] is None
    empty = (await client.get(URL, headers=admin, params=filters | {"source": "absent"})).json()
    assert empty["revenue"] == empty["periods"] == []
    for changes in (
        {"group_by": "year"},
        {"date_to": params["date_from"]},
        {"date_from": "2026-01-02T00:00:00"},
    ):
        assert (await client.get(URL, headers=admin, params=params | changes)).status_code == 422


async def test_sections_share_snapshot(client, session_maker, monkeypatch):
    await seed_overview(client, session_maker)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    original = overview.referral_report

    async def insert_between_sections(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with session_maker() as writer:
            user_id = await writer.scalar(select(Lead.user_id).limit(1))
            writer.add(Lead(user_id=user_id, source="concurrent"))
            await writer.commit()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(overview, "referral_report", insert_between_sections)
        first = await client.get(URL, headers=admin)
    assert first.json()["funnel"]["total_leads"] == 1
    assert (await client.get(URL, headers=admin)).json()["funnel"]["total_leads"] == 2
