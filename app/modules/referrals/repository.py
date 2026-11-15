import hashlib
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.referrals.models import ReferralClick, ReferralCode, ReferralProgram
from app.modules.referrals.schemas import ReferralClickCreate


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


class ReferralProgramRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, program: ReferralProgram) -> None:
        self.session.add(program)

    async def list(self, limit: int, offset: int) -> list[ReferralProgram]:
        statement = select(ReferralProgram).order_by(ReferralProgram.id).limit(limit).offset(offset)
        return list((await self.session.scalars(statement)).all())

    async def get_for_update(self, program_id: UUID) -> ReferralProgram | None:
        statement = (
            select(ReferralProgram).where(ReferralProgram.id == program_id).with_for_update()
        )
        return await self.session.scalar(statement)


class ReferralClickRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_request(self, request_id: UUID) -> None:
        digest = hashlib.blake2s(request_id.bytes, digest_size=4).digest()
        key = int.from_bytes(digest, byteorder="big", signed=True)
        await self.session.execute(select(func.pg_advisory_xact_lock(0x52454643, key)))

    async def get_by_request_id(self, request_id: UUID) -> tuple[ReferralClick, str] | None:
        statement = (
            select(ReferralClick, ReferralCode.code)
            .join(ReferralCode, ReferralCode.id == ReferralClick.referral_code_id)
            .where(ReferralClick.request_id == request_id)
        )
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1]) if row else None

    async def lock_code_for_read(self, code: str) -> ReferralCode | None:
        statement = select(ReferralCode).where(ReferralCode.code == code).with_for_update(read=True)
        return await self.session.scalar(statement)

    async def insert_if_new(self, code_id: UUID, data: ReferralClickCreate) -> ReferralClick | None:
        statement = (
            insert(ReferralClick)
            .values(
                referral_code_id=code_id,
                visitor_id=data.visitor_id,
                request_id=data.request_id,
                source=data.source,
                campaign=data.campaign,
            )
            .on_conflict_do_nothing(constraint="uq_referral_clicks_request_id")
            .returning(ReferralClick)
        )
        return await self.session.scalar(statement)
