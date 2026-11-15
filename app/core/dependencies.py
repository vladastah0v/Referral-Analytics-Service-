from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.security import InvalidTokenError, decode_token
from app.modules.auth.repository import AuthSessionRepository
from app.modules.users.models import User, UserRole

SessionDependency = Annotated[AsyncSession, Depends(get_session)]
bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    session: SessionDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if credentials is None:
        raise AuthenticationError
    try:
        claims = decode_token(credentials.credentials, "access")
    except InvalidTokenError as exc:
        raise AuthenticationError from exc
    user = await AuthSessionRepository(session).get_active_user(
        claims.session_id, claims.user_id, datetime.now(UTC)
    )
    if user is None:
        raise AuthenticationError
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable:
    async def authorize(user: CurrentUser) -> User:
        if user.role not in roles:
            raise PermissionDeniedError
        return user

    return authorize
