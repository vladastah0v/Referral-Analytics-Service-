import secrets
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.referrals.models import (
    ReferralAttribution,
    ReferralClick,
    ReferralCode,
    ReferralProgram,
)
from app.modules.referrals.repository import (
    ReferralAttributionRepository,
    ReferralClickRepository,
    ReferralCodeRepository,
    ReferralProgramRepository,
    ReferralStatisticsRepository,
)
from app.modules.referrals.schemas import (
    ReferralClickCreate,
    ReferralCodeCreate,
    ReferralCodeUpdate,
    ReferralProgramCreate,
    ReferralProgramUpdate,
    ReferralStatistics,
)
from app.modules.users.models import User


class ReferralCodeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.codes = ReferralCodeRepository(session)

    async def create(self, owner_id: UUID, data: ReferralCodeCreate) -> ReferralCode:
        for _ in range(5):
            code = secrets.token_urlsafe(18)
            record = await self.codes.insert_if_unique(
                owner_id, code, data.expires_at, data.max_uses
            )
            if record is not None:
                await self.session.commit()
                return record
        raise RuntimeError("Unable to generate a unique referral code")

    async def list_for_owner(self, owner_id: UUID, limit: int, offset: int) -> list[ReferralCode]:
        return await self.codes.list_for_owner(owner_id, limit, offset)

    async def get_for_owner(self, code_id: UUID, owner_id: UUID) -> ReferralCode | None:
        return await self.codes.get_for_owner(code_id, owner_id)

    async def update_for_owner(
        self, code_id: UUID, owner_id: UUID, data: ReferralCodeUpdate
    ) -> ReferralCode | None:
        record = await self.codes.update_for_owner(
            code_id, owner_id, data.model_dump(exclude_unset=True)
        )
        if record is not None:
            await self.session.commit()
        return record


class InvalidProgramUpdateError(Exception):
    pass


class ReferralProgramService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.programs = ReferralProgramRepository(session)

    async def create(self, data: ReferralProgramCreate) -> ReferralProgram:
        program = ReferralProgram(**data.model_dump())
        self.programs.add(program)
        await self.session.commit()
        return program

    async def list(self, limit: int, offset: int) -> list[ReferralProgram]:
        return await self.programs.list(limit, offset)

    async def update(self, program_id: UUID, data: ReferralProgramUpdate) -> ReferralProgram | None:
        program = await self.programs.get_for_update(program_id)
        if program is None:
            return None
        changes = data.model_dump(exclude_unset=True)
        current = {field: getattr(program, field) for field in ReferralProgramCreate.model_fields}
        try:
            validated = ReferralProgramCreate.model_validate(current | changes)
        except ValidationError as exc:
            raise InvalidProgramUpdateError from exc
        for field in changes:
            setattr(program, field, getattr(validated, field))
        await self.session.commit()
        return program


class ReferralCodeUnavailableError(Exception):
    pass


class ReferralClickConflictError(Exception):
    pass


class ReferralClickService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.clicks = ReferralClickRepository(session)

    @staticmethod
    def _verify_replay(
        existing: tuple[ReferralClick, str], data: ReferralClickCreate
    ) -> ReferralClick:
        click, code = existing
        if (
            code != data.code
            or click.visitor_id != data.visitor_id
            or click.source != data.source
            or click.campaign != data.campaign
        ):
            raise ReferralClickConflictError
        return click

    async def track(self, data: ReferralClickCreate) -> tuple[ReferralClick, bool]:
        await self.clicks.lock_request(data.request_id)
        await self.clicks.lock_visitor(data.visitor_id)
        existing = await self.clicks.get_by_request_id(data.request_id)
        if existing is not None:
            return self._verify_replay(existing, data), False

        code = await self.clicks.lock_code_for_read(data.code)
        if (
            code is None
            or not code.is_active
            or (code.expires_at is not None and code.expires_at <= await self.clicks.current_time())
        ):
            raise ReferralCodeUnavailableError

        click = await self.clicks.insert_if_new(code.id, data)
        if click is None:
            existing = await self.clicks.get_by_request_id(data.request_id)
            if existing is None:
                raise RuntimeError("Conflicting referral click could not be loaded")
            return self._verify_replay(existing, data), False
        await self.session.commit()
        return click, True


class ReferralAttributionService:
    """Attribute an existing registration within the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.attributions = ReferralAttributionRepository(session)

    async def attribute_registration(
        self, user_id: UUID, visitor_id: UUID
    ) -> ReferralAttribution | None:
        await self.attributions.lock_user(user_id)
        existing = await self.attributions.get_for_user(user_id)
        if existing is not None:
            return existing
        await self.attributions.lock_visitor(visitor_id)
        user = await self.session.get(User, user_id)
        if user is None:
            raise ValueError("User not found")
        registered_at = user.created_at
        cursor = None
        while True:
            candidates = await self.attributions.candidates(
                visitor_id, user_id, registered_at - timedelta(days=30), registered_at, cursor
            )
            if not candidates:
                return None
            for click_id, code_id, clicked_at in candidates:
                cursor = (clicked_at, click_id)
                # Rolling back a rejected candidate releases its row lock before trying another.
                async with self.session.begin_nested() as savepoint:
                    code = await self.attributions.lock_code(code_id)
                    if code is None or not code.is_active or code.owner_id == user_id:
                        await savepoint.rollback()
                        continue
                    if (
                        code.expires_at is not None
                        and code.expires_at <= await self.attributions.current_time()
                    ):
                        await savepoint.rollback()
                        continue
                    if code.max_uses is not None:
                        uses = await self.attributions.count_code_uses(code.id)
                        if uses >= code.max_uses:
                            await savepoint.rollback()
                            continue
                    attribution = await self.attributions.insert_if_new(code, click_id, user_id)
                    if attribution is None:
                        await savepoint.rollback()
                        return await self.attributions.get_for_user(user_id)
                    return attribution


class InvalidStatisticsRangeError(Exception):
    pass


class ReferralStatisticsService:
    def __init__(self, session: AsyncSession) -> None:
        self.statistics = ReferralStatisticsRepository(session)

    async def get_for_owner(
        self, owner_id: UUID, date_from: datetime | None, date_to: datetime | None
    ) -> ReferralStatistics:
        date_from = date_from.astimezone(UTC) if date_from is not None else None
        date_to = date_to.astimezone(UTC) if date_to is not None else None
        if date_from is not None and date_to is not None and date_from >= date_to:
            raise InvalidStatisticsRangeError
        clicks, visitors, registrations = await self.statistics.totals(owner_id, date_from, date_to)
        conversion = (
            (Decimal(registrations) * Decimal(100) / Decimal(visitors)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if visitors
            else None
        )
        return ReferralStatistics(
            date_from=date_from,
            date_to=date_to,
            total_clicks=clicks,
            unique_visitors=visitors,
            referred_registrations=registrations,
            conversion_rate=conversion,
        )
