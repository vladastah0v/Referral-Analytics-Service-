import secrets
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.referrals.models import ReferralCode
from app.modules.referrals.repository import ReferralCodeRepository
from app.modules.referrals.schemas import ReferralCodeCreate


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
