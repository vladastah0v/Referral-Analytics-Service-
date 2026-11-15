import secrets
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.referrals.models import ReferralCode
from app.modules.referrals.repository import ReferralCodeRepository
from app.modules.referrals.schemas import ReferralCodeCreate, ReferralCodeUpdate


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
