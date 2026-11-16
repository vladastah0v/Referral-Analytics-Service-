import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.modules.rewards.models import ReferralRewardHistory
from app.modules.users.models import User, UserRole
from tests.integration.test_auth import PASSWORD, bearer, login
from tests.integration.test_referral_codes import authenticated
from tests.integration.test_referral_programs import staff_headers
from tests.integration.test_reward_accrual import process, setup_order, state

ME = "/api/v1/rewards/me"
ADMIN = "/api/v1/admin/rewards"


async def accrued(engine, factory):
    values, now = await setup_order(engine)
    await process(factory, values, now)
    reward, _ = await state(factory)
    return values, now, reward


async def test_roles_and_own_reward_privacy(client, db_engine, session_maker):
    values, _, reward = await accrued(db_engine, session_maker)
    async with session_maker() as session:
        owner = await session.get(User, values["referrer_id"])
        owner.password_hash = await hash_password(PASSWORD)
        email = owner.email
        await session.commit()
    owner_headers = bearer((await login(client, email)).json()["access_token"])
    outsider = await authenticated(client, "outsider@example.com")
    manager = await staff_headers(client, session_maker, UserRole.MANAGER)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    url = f"{ADMIN}/{reward.id}/approve"
    for headers, code in (({}, 401), (owner_headers, 403), (outsider, 403), (manager, 403)):
        assert (await client.get(ADMIN, headers=headers)).status_code == code
        assert (
            await client.patch(url, headers=headers, json={"expected_revision": 1})
        ).status_code == code
    assert (await client.get(ME)).status_code == 401
    response = await client.get(ME, headers=owner_headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    row = response.json()["items"][0]
    assert row["id"] == str(reward.id) and row["reward_amount"] == "100.00"
    assert row["revision"] == 1
    assert set(row) == {
        "id",
        "referral_program_id",
        "reward_amount",
        "currency",
        "status",
        "created_at",
        "approved_at",
        "revision",
    }
    for headers in (outsider, manager, admin):
        assert (await client.get(ME, headers=headers)).json()["items"] == []
    assert (await client.get(ADMIN, headers=admin)).json()["items"][0]["referrer_id"] == str(
        values["referrer_id"]
    )


async def test_filters_pagination_and_validation(client, db_engine, session_maker):
    values, _, reward = await accrued(db_engine, session_maker)
    other, now = await setup_order(db_engine)
    await process(session_maker, other, now, op="other-payment")
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    all_items = (await client.get(ADMIN, headers=admin)).json()["items"]
    for offset in (0, 1):
        result = (
            await client.get(ADMIN, headers=admin, params={"limit": 1, "offset": offset})
        ).json()
        assert result["items"] == [all_items[offset]]
    assert len(all_items) == 2
    for params in ({"currency": "USD"}, {"status": "approved"}, {"referrer_id": str(uuid4())}):
        assert (await client.get(ADMIN, headers=admin, params=params)).json()["items"] == []
    result = await client.get(
        ADMIN,
        headers=admin,
        params={"referrer_id": str(values["referrer_id"]), "status": "pending", "currency": "KZT"},
    )
    assert result.json()["items"][0]["id"] == str(reward.id)
    for params in (
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"status": "unknown"},
        {"currency": "kzt"},
    ):
        for endpoint in (ME, ADMIN):
            assert (await client.get(endpoint, headers=admin, params=params)).status_code == 422
    for payload in (
        {},
        {"expected_revision": 0},
        {"expected_revision": True},
        {"expected_revision": "1"},
        {"expected_revision": 1, "reward_amount": "1"},
    ):
        assert (
            await client.patch(f"{ADMIN}/{reward.id}/approve", headers=admin, json=payload)
        ).status_code == 422
    assert (
        await client.patch(
            f"{ADMIN}/{uuid4()}/approve", headers=admin, json={"expected_revision": 1}
        )
    ).status_code == 404


async def test_concurrent_approval_idempotency_and_history(client, db_engine, session_maker):
    _, _, reward = await accrued(db_engine, session_maker)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    url = f"{ADMIN}/{reward.id}/approve"
    responses = await asyncio.gather(
        *(client.patch(url, headers=admin, json={"expected_revision": 1}) for _ in range(2))
    )
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    row = responses[0].json()
    assert row["status"] == "approved" and row["approved_at"] and row["revision"] == 2
    reward, history = await state(session_maker)
    assert len(history) == 2 and history[-1].reason == "approved"
    assert history[-1].actor_id is not None and history[-1].financial_event_id is None
    async with session_maker() as session:
        assert (await session.get(User, history[-1].actor_id)).role == UserRole.ADMIN
    assert (
        await client.patch(url, headers=admin, json={"expected_revision": 2})
    ).status_code == 200
    assert len((await state(session_maker))[1]) == 2


async def test_refund_invalidates_stale_approval_and_cancellation(client, db_engine, session_maker):
    values, now, reward = await accrued(db_engine, session_maker)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    url = f"{ADMIN}/{reward.id}/approve"
    assert (
        await client.patch(url, headers=admin, json={"expected_revision": 1})
    ).status_code == 200
    await process(
        session_maker, values, now + timedelta(seconds=1), "order_refunded", "500.00", "refund1"
    )
    assert (
        await client.patch(url, headers=admin, json={"expected_revision": 1})
    ).status_code == 409
    response = await client.patch(url, headers=admin, json={"expected_revision": 3})
    assert response.status_code == 200 and response.json()["reward_amount"] == "50.00"
    await process(
        session_maker, values, now + timedelta(seconds=2), "order_refunded", "500.00", "refund2"
    )
    assert (
        await client.patch(url, headers=admin, json={"expected_revision": 5})
    ).status_code == 409
    reward, history = await state(session_maker)
    assert reward.status == "cancelled" and reward.approved_at is None and len(history) == 5


async def test_refund_racing_approval_cannot_approve_new_amount(client, db_engine, session_maker):
    values, now, reward = await accrued(db_engine, session_maker)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    response, _ = await asyncio.gather(
        client.patch(f"{ADMIN}/{reward.id}/approve", headers=admin, json={"expected_revision": 1}),
        process(
            session_maker, values, now + timedelta(seconds=1), "order_refunded", "500.00", "refund"
        ),
    )
    assert response.status_code in (200, 409)
    reward, history = await state(session_maker)
    assert reward.status == "pending" and reward.approved_at is None
    assert str(reward.reward_amount) == "50.00"
    assert history[-1].reason == "financial_recalculation"


async def test_approval_history_failure_rolls_back(client, db_engine, session_maker, monkeypatch):
    _, _, reward = await accrued(db_engine, session_maker)
    admin = await staff_headers(client, session_maker, UserRole.ADMIN)
    original = AsyncSession.flush

    async def fail(self, *args, **kwargs):
        approving = any(
            isinstance(item, ReferralRewardHistory) and item.reason == "approved"
            for item in self.new
        )
        await original(self, *args, **kwargs)
        if approving:
            raise RuntimeError("after history flush")

    with monkeypatch.context() as scoped:
        scoped.setattr(AsyncSession, "flush", fail)
        with pytest.raises(RuntimeError):
            await client.patch(
                f"{ADMIN}/{reward.id}/approve", headers=admin, json={"expected_revision": 1}
            )
    reward, history = await state(session_maker)
    assert reward.status == "pending" and reward.approved_at is None and len(history) == 1
