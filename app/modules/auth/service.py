import hmac
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    InvalidTokenError,
    create_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.modules.auth.models import AuthSession
from app.modules.auth.repository import AuthSessionRepository
from app.modules.auth.schemas import LoginRequest, TokenPair
from app.modules.users.models import User
from app.modules.users.repository import UserRepository


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sessions = AuthSessionRepository(session)
        self.users = UserRepository(session)

    async def login(self, data: LoginRequest) -> TokenPair:
        user = await self.users.get_by_email(data.email)
        user_id = user.id if user is not None else None
        candidate_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
        password = data.password.get_secret_value()
        for _ in range(2):
            # Release the connection before waiting for Argon2.
            await self.session.rollback()
            if not await verify_password(candidate_hash, password) or user_id is None:
                raise AuthenticationError
            replacement = (
                await hash_password(password) if password_needs_rehash(candidate_hash) else None
            )
            user = await self.session.scalar(
                select(User).where(User.id == user_id, User.is_active.is_(True)).with_for_update()
            )
            if user is None:
                await self.session.rollback()
                raise AuthenticationError
            if user.password_hash == candidate_hash:
                break
            # A concurrent login can replace an old hash for the same password.
            candidate_hash = user.password_hash
        else:
            await self.session.rollback()
            raise AuthenticationError
        if replacement is not None:
            user.password_hash = replacement

        session_id = uuid4()
        refresh_token, expires_at = create_token(user.id, session_id, "refresh")
        self.sessions.add(
            AuthSession(
                id=session_id,
                user_id=user.id,
                refresh_token_hash=hash_refresh_token(refresh_token),
                expires_at=expires_at,
            )
        )
        access_token, _ = create_token(user.id, session_id, "access")
        await self.session.commit()
        return self._token_pair(access_token, refresh_token)

    async def rotate(self, refresh_token: str) -> TokenPair:
        auth_session, user_id = await self._lock_valid_session(refresh_token)
        new_refresh_token, expires_at = create_token(user_id, auth_session.id, "refresh")
        auth_session.refresh_token_hash = hash_refresh_token(new_refresh_token)
        auth_session.expires_at = expires_at
        access_token, _ = create_token(user_id, auth_session.id, "access")
        await self.session.commit()
        return self._token_pair(access_token, new_refresh_token)

    async def logout(self, refresh_token: str) -> None:
        auth_session, _ = await self._lock_valid_session(refresh_token, require_active_user=False)
        auth_session.revoked_at = datetime.now(UTC)
        await self.session.commit()

    async def _lock_valid_session(
        self, refresh_token: str, *, require_active_user: bool = True
    ) -> tuple[AuthSession, UUID]:
        try:
            claims = decode_token(refresh_token, "refresh")
        except InvalidTokenError as exc:
            raise AuthenticationError from exc
        result = await self.sessions.get_for_update(claims.session_id)
        if result is None:
            raise AuthenticationError
        auth_session, user = result
        if (
            user.id != claims.user_id
            or (require_active_user and not user.is_active)
            or auth_session.revoked_at is not None
            or auth_session.expires_at <= datetime.now(UTC)
            or not hmac.compare_digest(
                auth_session.refresh_token_hash, hash_refresh_token(refresh_token)
            )
        ):
            raise AuthenticationError
        return auth_session, user.id

    @staticmethod
    def _token_pair(access_token: str, refresh_token: str) -> TokenPair:
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=get_settings().jwt_access_ttl_minutes * 60,
        )
