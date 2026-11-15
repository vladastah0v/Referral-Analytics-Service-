from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import AuthSession
from app.modules.users.models import User


class AuthSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, auth_session: AuthSession) -> None:
        self.session.add(auth_session)

    async def get_for_update(self, session_id: UUID) -> tuple[AuthSession, User] | None:
        statement = (
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.id == session_id)
            .with_for_update(of=AuthSession)
        )
        row = (await self.session.execute(statement)).one_or_none()
        return (row[0], row[1]) if row else None

    async def get_active_user(self, session_id: UUID, user_id: UUID, now: datetime) -> User | None:
        statement = (
            select(User)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                AuthSession.id == session_id,
                User.id == user_id,
                User.is_active.is_(True),
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )
        return await self.session.scalar(statement)
