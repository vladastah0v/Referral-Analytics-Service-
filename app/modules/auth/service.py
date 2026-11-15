import hmac
from datetime import UTC, datetime
from uuid import UUID, uuid4

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
from app.modules.users.repository import UserRepository


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sessions = AuthSessionRepository(session)
        self.users = UserRepository(session)

    async def login(self, data: LoginRequest) -> TokenPair:
        user = await self.users.get_by_email(data.email)
        candidate_hash = user.password_hash if user else DUMMY_PASSWORD_HASH
        password_valid = await verify_password(candidate_hash, data.password.get_secret_value())
        if user is None or not password_valid or not user.is_active:
            raise AuthenticationError
        if password_needs_rehash(user.password_hash):
            user.password_hash = await hash_password(data.password.get_secret_value())

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
