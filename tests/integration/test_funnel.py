from sqlalchemy import select, update

from app.modules.leads.models import Lead
from app.modules.referrals.models import ReferralAttribution, ReferralClick, ReferralCode
from app.modules.users.models import UserRole
from tests.integration.test_lead_creation import own_user_id
from tests.integration.test_referral_programs import staff_headers
from tests.integration.test_referral_statistics import seed_statistics, stamp

URL = "/api/v1/analytics/funnel"


async def test_lead_cohort_cutoff_grouping_and_manager_scope(client, session_maker):
    customer, _, _ = await seed_statistics(client, session_maker)
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    manager_id = await own_user_id(client, manager)
    customer_id = await own_user_id(client, customer)

    async with session_maker() as session:
        # Multiple leads from the same customer must count separately.
        a = Lead(
            user_id=customer_id,
            source="web",
            assigned_manager_id=manager_id,
            status="converted",
            created_at=stamp(1),
            qualified_at=stamp(2),
            converted_at=stamp(3),
        )
        b = Lead(
            user_id=customer_id,
            source="web",
            assigned_manager_id=manager_id,
            status="lost",
            created_at=stamp(2),
            qualified_at=stamp(2, 12),
        )
        c = Lead(
            user_id=customer_id,
            source="phone",
            status="converted",
            created_at=stamp(2),
            qualified_at=stamp(3),
            converted_at=stamp(4),
        )
        d = Lead(user_id=customer_id, source="phone", created_at=stamp(3))
        session.add_all([a, b, c, d])
        await session.commit()
        lead_id = a.id

    for headers, expected in (({}, 401), (customer, 403)):
        assert (await client.get(URL, headers=headers)).status_code == expected

    params = {"date_from": "2026-01-01T05:00:00+05:00", "date_to": stamp(3).isoformat()}
    response = await client.get(URL, headers=admin, params=params)
    assert response.status_code == 200, response.text
    data = response.json()
    assert response.headers["cache-control"] == "private, no-store"
    assert data == dict(
        date_from="2026-01-01T00:00:00Z",
        date_to="2026-01-03T00:00:00Z",
        group_by=None,
        total_leads=3,
        qualified_leads=2,
        converted_leads=0,
        lead_conversion_rate="0.00",
        periods=[],
    )
    for group, size in (("day", 2), ("week", 1), ("month", 1)):
        report = (await client.get(URL, headers=admin, params=params | {"group_by": group})).json()
        assert len(report["periods"]) == size
        assert sum(p["total_leads"] for p in report["periods"]) == report["total_leads"] == 3
        if group == "day":
            assert [(p["total_leads"], p["qualified_leads"]) for p in report["periods"]] == [
                (1, 1),
                (2, 1),
            ]
        if group == "week":
            assert report["periods"][0]["period_start"] == "2025-12-29T00:00:00Z"
    data = (await client.get(URL, headers=manager)).json()
    assert (
        data["total_leads"],
        data["qualified_leads"],
        data["converted_leads"],
        data["lead_conversion_rate"],
    ) == (2, 2, 1, "50.00")
    async with session_maker() as session:
        await session.execute(
            update(Lead).where(Lead.id == lead_id).values(assigned_manager_id=None)
        )
        await session.commit()
    assert (await client.get(URL, headers=manager)).json()["converted_leads"] == 0
    data = (await client.get(URL, headers=admin, params={"date_from": stamp(2).isoformat()})).json()
    assert data["lead_conversion_rate"] == "33.33"


async def test_funnel_attribution_filters_and_empty_results(client, session_maker):
    await seed_statistics(client, session_maker)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    async with session_maker() as session:
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
            .values(campaign="winter", source="mail")
        )
        for source, attr_id in (("web", attr.id), ("phone", attr.id), ("web", None)):
            session.add(
                Lead(user_id=attr.referred_user_id, source=source, referral_attribution_id=attr_id)
            )
        await session.commit()
    filters = {"source": "web", "campaign": "winter", "referral_code": code}
    for params, count in (
        ({}, 3),
        ({"source": "web"}, 2),
        ({"campaign": "winter"}, 2),
        ({"referral_code": code}, 2),
        (filters, 1),
    ):
        assert (await client.get(URL, headers=admin, params=params)).json()["total_leads"] == count
    for changes in ({"source": "mail"}, {"referral_code": "unknown"}, {"campaign": "other"}):
        empty = (
            await client.get(URL, headers=admin, params=filters | changes | {"group_by": "day"})
        ).json()
        assert (
            empty["total_leads"] == 0
            and empty["lead_conversion_rate"] is None
            and empty["periods"] == []
        )
    for invalid in (
        {"date_from": stamp(2).isoformat(), "date_to": stamp(2).isoformat()},
        {"date_to": "2026-01-02T00:00:00"},
        {"group_by": "year"},
        {"source": ""},
    ):
        assert (await client.get(URL, headers=admin, params=invalid)).status_code == 422
