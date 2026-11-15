import hashlib
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.referrals.models import (
    AttributionModel,
    ReferralAttribution,
    ReferralClick,
    ReferralCode,
    ReferralProgram,
)
from app.modules.referrals.schemas import ReferralClickCreate


async def _lock_visitor(session: AsyncSession, visitor_id: UUID) -> None:
    # Clicks and registrations use the same transaction-scoped visitor lock.
    digest = hashlib.blake2s(visitor_id.bytes, digest_size=4).digest()
    key = int.from_bytes(digest, byteorder="big", signed=True)
    await session.execute(select(func.pg_advisory_xact_lock(0x56495349, key)))


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

    async def lock_visitor(self, visitor_id: UUID) -> None:
        await _lock_visitor(self.session, visitor_id)

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

    async def current_time(self) -> datetime:
        return await self.session.scalar(select(func.clock_timestamp()))

    async def insert_if_new(self, code_id: UUID, data: ReferralClickCreate) -> ReferralClick | None:
        statement = (
            insert(ReferralClick)
            .values(
                referral_code_id=code_id,
                visitor_id=data.visitor_id,
                request_id=data.request_id,
                clicked_at=func.clock_timestamp(),
                source=data.source,
                campaign=data.campaign,
            )
            .on_conflict_do_nothing(constraint="uq_referral_clicks_request_id")
            .returning(ReferralClick)
        )
        return await self.session.scalar(statement)


class ReferralAttributionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_user(self, user_id: UUID) -> None:
        digest = hashlib.blake2s(user_id.bytes, digest_size=4).digest()
        key = int.from_bytes(digest, byteorder="big", signed=True)
        await self.session.execute(select(func.pg_advisory_xact_lock(0x41545452, key)))

    async def lock_visitor(self, visitor_id: UUID) -> None:
        await _lock_visitor(self.session, visitor_id)

    async def get_for_user(self, user_id: UUID) -> ReferralAttribution | None:
        statement = select(ReferralAttribution).where(
            ReferralAttribution.referred_user_id == user_id
        )
        return await self.session.scalar(statement)

    async def candidates(
        self,
        visitor_id: UUID,
        user_id: UUID,
        start: datetime,
        end: datetime,
        cursor: tuple[datetime, UUID] | None,
        limit: int = 100,
    ) -> list[tuple[UUID, UUID, datetime]]:
        ranked = (
            select(
                ReferralClick.id.label("click_id"),
                ReferralClick.referral_code_id.label("code_id"),
                ReferralClick.clicked_at.label("clicked_at"),
                func.row_number()
                .over(
                    partition_by=ReferralClick.referral_code_id,
                    order_by=(ReferralClick.clicked_at.desc(), ReferralClick.id.desc()),
                )
                .label("position"),
            )
            .where(
                ReferralClick.visitor_id == visitor_id,
                ReferralClick.clicked_at >= start,
                ReferralClick.clicked_at <= end,
            )
            .subquery()
        )
        uses = (
            select(func.count())
            .select_from(ReferralAttribution)
            .where(ReferralAttribution.referral_code_id == ReferralCode.id)
            .correlate(ReferralCode)
            .scalar_subquery()
        )
        statement = (
            select(ranked.c.click_id, ranked.c.code_id, ranked.c.clicked_at)
            .join(ReferralCode, ReferralCode.id == ranked.c.code_id)
            .where(
                ranked.c.position == 1,
                ReferralCode.is_active.is_(True),
                ReferralCode.owner_id != user_id,
                or_(
                    ReferralCode.expires_at.is_(None),
                    ReferralCode.expires_at > func.clock_timestamp(),
                ),
                or_(ReferralCode.max_uses.is_(None), uses < ReferralCode.max_uses),
            )
        )
        if cursor is not None:
            clicked_at, click_id = cursor
            statement = statement.where(
                or_(
                    ranked.c.clicked_at < clicked_at,
                    and_(ranked.c.clicked_at == clicked_at, ranked.c.click_id < click_id),
                )
            )
        statement = statement.order_by(ranked.c.clicked_at.desc(), ranked.c.click_id.desc()).limit(
            limit
        )
        return [
            (row.click_id, row.code_id, row.clicked_at)
            for row in (await self.session.execute(statement))
        ]

    async def lock_code(self, code_id: UUID) -> ReferralCode | None:
        statement = (
            select(ReferralCode)
            .where(ReferralCode.id == code_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(statement)

    async def current_time(self) -> datetime:
        return await self.session.scalar(select(func.clock_timestamp()))

    async def count_code_uses(self, code_id: UUID) -> int:
        statement = (
            select(func.count())
            .select_from(ReferralAttribution)
            .where(ReferralAttribution.referral_code_id == code_id)
        )
        return await self.session.scalar(statement)

    async def insert_if_new(
        self, code: ReferralCode, click_id: UUID, user_id: UUID
    ) -> ReferralAttribution | None:
        statement = (
            insert(ReferralAttribution)
            .values(
                referral_code_id=code.id,
                referrer_id=code.owner_id,
                referred_user_id=user_id,
                click_id=click_id,
                attribution_model=AttributionModel.LAST_REFERRAL_CLICK,
            )
            .on_conflict_do_nothing(constraint="uq_referral_attributions_referred_user")
            .returning(ReferralAttribution)
        )
        return await self.session.scalar(statement)
