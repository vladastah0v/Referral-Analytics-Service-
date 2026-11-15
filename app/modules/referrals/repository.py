from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.referrals.models import ReferralCode


class ReferralCodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_if_unique(
        self, owner_id: UUID, code: str, expires_at: datetime | None, max_uses: int | None
    ) -> ReferralCode | None:
        statement = (
            insert(ReferralCode)
            .values(owner_id=owner_id, code=code, expires_at=expires_at, max_uses=max_uses)
            .on_conflict_do_nothing(constraint="uq_referral_codes_code")
            .returning(ReferralCode)
        )
        return await self.session.scalar(statement)

    async def list_for_owner(self, owner_id: UUID, limit: int, offset: int) -> list[ReferralCode]:
        statement = (
            select(ReferralCode)
            .where(ReferralCode.owner_id == owner_id)
            .order_by(ReferralCode.created_at.desc(), ReferralCode.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.scalars(statement)).all())

    async def get_for_owner(self, code_id: UUID, owner_id: UUID) -> ReferralCode | None:
        return await self.session.scalar(
            select(ReferralCode).where(
                ReferralCode.id == code_id, ReferralCode.owner_id == owner_id
            )
        )

    async def update_for_owner(
        self, code_id: UUID, owner_id: UUID, changes: dict
    ) -> ReferralCode | None:
        statement = (
            update(ReferralCode)
            .where(ReferralCode.id == code_id, ReferralCode.owner_id == owner_id)
            .values(**changes)
            .returning(ReferralCode)
        )
        return await self.session.scalar(statement)
