from fastapi import APIRouter, Response, status

from app.core.dependencies import SessionDependency
from app.modules.auth.schemas import LoginRequest, RefreshRequest, TokenPair
from app.modules.auth.service import AuthService
from app.modules.users.models import UserRole
from app.modules.users.schemas import UserRead, UserRegistration
from app.modules.users.service import UserService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(data: UserRegistration, session: SessionDependency) -> UserRead:
    user = await UserService(session).create(data, UserRole.CUSTOMER, visitor_id=data.visitor_id)
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPair)
async def login(data: LoginRequest, session: SessionDependency, response: Response) -> TokenPair:
    tokens = await AuthService(session).login(data)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return tokens


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    data: RefreshRequest, session: SessionDependency, response: Response
) -> TokenPair:
    tokens = await AuthService(session).rotate(data.refresh_token.get_secret_value())
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshRequest, session: SessionDependency) -> Response:
    await AuthService(session).logout(data.refresh_token.get_secret_value())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
