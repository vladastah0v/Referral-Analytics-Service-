from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EmailAlreadyRegisteredError
from app.core.security import hash_password
from app.modules.referrals.service import ReferralAttributionService
from app.modules.users.models import User, UserRole
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import UserCreate


def _is_duplicate_email(error: IntegrityError) -> bool:
    current = error.orig
    while current is not None:
        if getattr(current, "constraint_name", None) == "uq_users_email":
            return True
        current = current.__cause__
    return False


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def create(
        self, data: UserCreate, role: UserRole, *, visitor_id: UUID | None = None
    ) -> User:
        if visitor_id is not None and role != UserRole.CUSTOMER:
            raise ValueError("Referral attribution is only available for customer registration")
        user = User(
            email=data.email,
            full_name=data.full_name,
            password_hash=await hash_password(data.password.get_secret_value()),
            role=role,
            is_active=True,
        )
        self.users.add(user)
        try:
            if visitor_id is not None:
                await self.session.flush()
                await ReferralAttributionService(self.session).attribute_registration(
                    user.id, visitor_id
                )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            if _is_duplicate_email(exc):
                raise EmailAlreadyRegisteredError from exc
            raise
        return user
