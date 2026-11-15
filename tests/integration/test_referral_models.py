from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.referrals.models import ReferralClick, ReferralCode, ReferralProgram
from app.modules.users.models import User


async def test_code_and_click_constraints(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        owner_id, code_id, request_id = uuid4(), uuid4(), uuid4()
        await connection.execute(
            insert(User).values(
                id=owner_id,
                email="referrer@example.com",
                password_hash="hash",
                full_name="Referrer",
                role="customer",
            )
        )
        await connection.execute(
            insert(ReferralCode).values(id=code_id, owner_id=owner_id, code="example", max_uses=1)
        )
        await connection.execute(
            insert(ReferralClick).values(
                id=uuid4(), referral_code_id=code_id, visitor_id=uuid4(), request_id=request_id
            )
        )
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    insert(ReferralClick).values(
                        id=uuid4(),
                        referral_code_id=code_id,
                        visitor_id=uuid4(),
                        request_id=request_id,
                    )
                )
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    insert(ReferralCode).values(id=uuid4(), owner_id=owner_id, code="example")
                )
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    insert(ReferralCode).values(
                        id=uuid4(), owner_id=owner_id, code="another", max_uses=0
                    )
                )
        await transaction.rollback()


@pytest.mark.parametrize(
    "changes",
    [
        {"reward_value": Decimal("0")},
        {"reward_type": "percentage", "reward_value": Decimal("100.01")},
        {"max_reward": Decimal("-1")},
        {"currency": "kzt"},
        {"ends_at": datetime(2020, 1, 1, tzinfo=UTC)},
    ],
)
async def test_program_constraints(db_engine: AsyncEngine, changes: dict) -> None:
    values = {
        "id": uuid4(),
        "name": "Referral",
        "reward_type": "fixed",
        "reward_value": Decimal("100.00"),
        "currency": "KZT",
        "starts_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(changes)
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(IntegrityError):
            await connection.execute(insert(ReferralProgram).values(**values))
        await transaction.rollback()


async def test_attribution_constraints_enforce_consistent_source(db_engine: AsyncEngine) -> None:
    from app.modules.referrals.models import ReferralAttribution

    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        owner_a, owner_b, referred_a, referred_b = (uuid4() for _ in range(4))
        for user_id in (owner_a, owner_b, referred_a, referred_b):
            await connection.execute(
                insert(User).values(
                    id=user_id,
                    email=f"{user_id}@example.com",
                    password_hash="hash",
                    full_name="Test",
                    role="customer",
                )
            )
        code_a, code_b, click_a, click_b = (uuid4() for _ in range(4))
        for code_id, owner_id in ((code_a, owner_a), (code_b, owner_b)):
            await connection.execute(
                insert(ReferralCode).values(id=code_id, owner_id=owner_id, code=str(code_id))
            )
        for click_id, code_id in ((click_a, code_a), (click_b, code_b)):
            await connection.execute(
                insert(ReferralClick).values(
                    id=click_id, referral_code_id=code_id, visitor_id=uuid4(), request_id=uuid4()
                )
            )
        valid = {
            "id": uuid4(),
            "referral_code_id": code_a,
            "referrer_id": owner_a,
            "referred_user_id": referred_a,
            "click_id": click_a,
            "attribution_model": "last_referral_click",
        }
        await connection.execute(insert(ReferralAttribution).values(**valid))
        invalid_cases = (
            {"id": uuid4()},  # The same referred user cannot be attributed twice.
            {"id": uuid4(), "referred_user_id": owner_a},  # Self-referral.
            {"id": uuid4(), "referred_user_id": referred_b, "referrer_id": owner_b},
            {"id": uuid4(), "referred_user_id": referred_b, "click_id": click_b},
            {"id": uuid4(), "referred_user_id": referred_b, "click_id": uuid4()},
            {"id": uuid4(), "referred_user_id": uuid4()},
        )
        for changes in invalid_cases:
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        insert(ReferralAttribution).values(**(valid | changes))
                    )
        await connection.execute(
            insert(ReferralAttribution).values(
                **(valid | {"id": uuid4(), "referred_user_id": referred_b})
            )
        )
        await transaction.rollback()
