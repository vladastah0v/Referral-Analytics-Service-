from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.dependencies import CurrentUser, SessionDependency
from app.modules.referrals.schemas import ReferralCodeCreate, ReferralCodePage, ReferralCodeRead
from app.modules.referrals.service import ReferralCodeService

router = APIRouter(prefix="/referrals", tags=["referrals"])


@router.post("/codes", response_model=ReferralCodeRead, status_code=status.HTTP_201_CREATED)
async def create_code(
    data: ReferralCodeCreate, user: CurrentUser, session: SessionDependency
) -> ReferralCodeRead:
    record = await ReferralCodeService(session).create(user.id, data)
    return ReferralCodeRead.model_validate(record)


@router.get("/codes", response_model=ReferralCodePage)
async def list_codes(
    user: CurrentUser,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
) -> ReferralCodePage:
    records = await ReferralCodeService(session).list_for_owner(user.id, limit, offset)
    return ReferralCodePage(
        items=[ReferralCodeRead.model_validate(record) for record in records],
        limit=limit,
        offset=offset,
    )
