from sqlalchemy import select, update

from app.modules.referrals.models import ReferralClick, ReferralCode
from app.modules.users.models import UserRole
from tests.integration.test_referral_programs import staff_headers
from tests.integration.test_referral_statistics import seed_statistics, stamp

URL = "/api/v1/analytics/referrals"


async def test_report_access_periods_and_distinct_totals(client, session_maker):
    customer, _, _ = await seed_statistics(client, session_maker)
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    for headers, expected in (({}, 401), (customer, 403), (manager, 403)):
        assert (await client.get(URL, headers=headers)).status_code == expected
    for grouping, buckets in (("day", 4), ("week", 1), ("month", 1), (None, 0)):
        response = await client.get(
            URL, headers=admin, params={"group_by": grouping} if grouping else {}
        )
        assert response.status_code == 200, response.text
        report = response.json()
        assert response.headers["cache-control"] == "private, no-store"
        assert (
            report["total_clicks"],
            report["unique_visitors"],
            report["referred_registrations"],
            report["conversion_rate"],
        ) == (6, 4, 3, "75.00")
        assert len(report["periods"]) == buckets
        if grouping == "day":
            assert sum(row["unique_visitors"] for row in report["periods"]) == 6
            assert report["periods"][1]["period_start"] == "2026-01-02T00:00:00Z"
            assert report["periods"][1]["total_clicks"] == 3
        if grouping == "week":
            assert report["periods"][0]["period_start"] == "2025-12-29T00:00:00Z"


async def test_attributed_click_filters_and_time_boundaries(client, session_maker):
    await seed_statistics(client, session_maker)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    async with session_maker() as session:
        await session.execute(
            update(ReferralClick)
            .where(ReferralClick.clicked_at == stamp(1))
            .values(campaign="winter", source="mail")
        )
        code = await session.scalar(
            select(ReferralCode.code)
            .join(ReferralClick, ReferralClick.referral_code_id == ReferralCode.id)
            .where(ReferralClick.clicked_at == stamp(1))
        )
        await session.commit()
    filters = {
        "campaign": "winter",
        "source": "mail",
        "referral_code": code,
        "group_by": "day",
        "date_from": "2026-01-01T17:00:00+05:00",
        "date_to": "2026-01-02T00:00:00Z",
    }
    report = (await client.get(URL, headers=admin, params=filters)).json()
    assert report["date_from"] == "2026-01-01T12:00:00Z"
    assert report["referred_registrations"] == 1 and report["total_clicks"] == 0
    assert report["periods"][0]["conversion_rate"] is None
    for changes in (
        {"source": "social"},
        {"campaign": "summer"},
        {"referral_code": "missing"},
        {"date_from": "2026-01-01T09:00:00Z", "date_to": "2026-01-01T12:00:00Z"},
    ):
        empty = (await client.get(URL, headers=admin, params=filters | changes)).json()
        assert empty["conversion_rate"] is None and empty["periods"] == []
        assert (
            empty["total_clicks"]
            == empty["unique_visitors"]
            == empty["referred_registrations"]
            == 0
        )
    for invalid in (
        {"group_by": "year"},
        {"date_from": "2026-01-01T17:00:00"},
        {"campaign": ""},
        {"date_from": filters["date_to"], "date_to": filters["date_to"]},
    ):
        assert (await client.get(URL, headers=admin, params=invalid)).status_code == 422
