from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.modules.leads.models import Lead, LeadStatus, LeadStatusHistory
from app.modules.referrals.models import (
    AttributionModel,
    ReferralAttribution,
    ReferralClick,
    ReferralCode,
)
from app.modules.users.models import User


async def add_user(connection: AsyncConnection):
    user_id = uuid4()
    await connection.execute(
        insert(User).values(
            id=user_id,
            email=f"{user_id}@example.com",
            password_hash="hash",
            full_name="Test",
            role="customer",
        )
    )
    return user_id


async def rejected(connection: AsyncConnection, statement) -> None:
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(statement)


async def test_lead_defaults_and_status_history(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        user_id = await add_user(connection)
        manager_id = await add_user(connection)
        lead_id = uuid4()
        await connection.execute(
            insert(Lead).values(
                id=lead_id, user_id=user_id, assigned_manager_id=manager_id, source="website"
            )
        )
        lead = (await connection.execute(select(Lead).where(Lead.id == lead_id))).one()
        assert lead.status == LeadStatus.NEW
        assert lead.referral_attribution_id is None
        assert lead.qualified_at is None and lead.converted_at is None
        assert lead.created_at.tzinfo is not None and lead.updated_at.tzinfo is not None
        await connection.execute(
            insert(LeadStatusHistory).values(
                id=uuid4(),
                lead_id=lead_id,
                previous_status=None,
                new_status=LeadStatus.NEW,
                changed_by=user_id,
            )
        )
        await connection.execute(
            update(Lead).where(Lead.id == lead_id).values(status=LeadStatus.CONTACTED)
        )
        await connection.execute(
            insert(LeadStatusHistory).values(
                id=uuid4(),
                lead_id=lead_id,
                previous_status=LeadStatus.NEW,
                new_status=LeadStatus.CONTACTED,
                changed_by=manager_id,
            )
        )
        history = (
            await connection.execute(
                select(LeadStatusHistory).where(LeadStatusHistory.lead_id == lead_id)
            )
        ).all()
        assert len(history) == 2
        assert all(row.changed_at.tzinfo is not None for row in history)
        await rejected(connection, delete(Lead).where(Lead.id == lead_id))
        await rejected(connection, delete(User).where(User.id == manager_id))
        await transaction.rollback()


async def test_lead_and_history_constraints(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        user_id = await add_user(connection)
        lead_id = uuid4()
        await connection.execute(insert(Lead).values(id=lead_id, user_id=user_id, source="website"))
        for source in ("", "   ", "\t\n"):
            await rejected(
                connection, insert(Lead).values(id=uuid4(), user_id=user_id, source=source)
            )
        await rejected(
            connection, insert(Lead).values(id=uuid4(), user_id=uuid4(), source="website")
        )
        await rejected(
            connection,
            insert(Lead).values(
                id=uuid4(), user_id=user_id, assigned_manager_id=uuid4(), source="website"
            ),
        )
        invalid_status = text(
            "INSERT INTO leads (id, user_id, source, status) "
            "VALUES (:id, :user_id, 'website', 'unknown')"
        ).bindparams(id=uuid4(), user_id=user_id)
        await rejected(connection, invalid_status)
        await rejected(
            connection,
            insert(LeadStatusHistory).values(
                id=uuid4(),
                lead_id=lead_id,
                previous_status=LeadStatus.NEW,
                new_status=LeadStatus.NEW,
                changed_by=user_id,
            ),
        )
        await rejected(
            connection,
            insert(LeadStatusHistory).values(
                id=uuid4(),
                lead_id=lead_id,
                previous_status=LeadStatus.NEW,
                new_status=LeadStatus.CONTACTED,
                changed_by=uuid4(),
            ),
        )
        await rejected(
            connection,
            insert(LeadStatusHistory).values(
                id=uuid4(),
                lead_id=uuid4(),
                previous_status=LeadStatus.NEW,
                new_status=LeadStatus.CONTACTED,
                changed_by=user_id,
            ),
        )
        invalid_history = text(
            "INSERT INTO lead_status_history "
            "(id, lead_id, previous_status, new_status, changed_by) "
            "VALUES (:id, :lead_id, 'new', 'unknown', :changed_by)"
        ).bindparams(id=uuid4(), lead_id=lead_id, changed_by=user_id)
        await rejected(connection, invalid_history)
        await transaction.rollback()


async def test_lead_attribution_belongs_to_same_user(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        referrer_id, referred_id, other_id = [await add_user(connection) for _ in range(3)]
        code_id, click_id, attribution_id = uuid4(), uuid4(), uuid4()
        await connection.execute(
            insert(ReferralCode).values(id=code_id, owner_id=referrer_id, code="source-code")
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
                referrer_id=referrer_id,
                referred_user_id=referred_id,
                click_id=click_id,
                attribution_model=AttributionModel.LAST_REFERRAL_CLICK,
            )
        )
        await connection.execute(
            insert(Lead).values(
                id=uuid4(),
                user_id=referred_id,
                referral_attribution_id=attribution_id,
                source="referral",
            )
        )
        await connection.execute(insert(Lead).values(id=uuid4(), user_id=other_id, source="direct"))
        await rejected(
            connection,
            insert(Lead).values(
                id=uuid4(),
                user_id=other_id,
                referral_attribution_id=attribution_id,
                source="referral",
            ),
        )
        await rejected(
            connection,
            insert(Lead).values(
                id=uuid4(), user_id=referred_id, referral_attribution_id=uuid4(), source="referral"
            ),
        )
        await rejected(
            connection, delete(ReferralAttribution).where(ReferralAttribution.id == attribution_id)
        )
        await transaction.rollback()
