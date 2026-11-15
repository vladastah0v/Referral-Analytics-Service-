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
