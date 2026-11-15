from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.core.dependencies import CurrentUser, SessionDependency
from app.modules.referrals.schemas import (
    ReferralClickCreate,
    ReferralClickRead,
    ReferralCodeCreate,
    ReferralCodePage,
    ReferralCodeRead,
    ReferralCodeUpdate,
)
from app.modules.referrals.service import (
    ReferralClickConflictError,
    ReferralClickService,
    ReferralCodeService,
    ReferralCodeUnavailableError,
)

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


@router.get("/codes/{code_id}", response_model=ReferralCodeRead)
async def get_code(
    code_id: UUID, user: CurrentUser, session: SessionDependency
) -> ReferralCodeRead:
    record = await ReferralCodeService(session).get_for_owner(code_id, user.id)
    if record is None:
        raise HTTPException(status_code=404, detail="Referral code not found")
    return ReferralCodeRead.model_validate(record)


@router.patch("/codes/{code_id}", response_model=ReferralCodeRead)
async def update_code(
    code_id: UUID, data: ReferralCodeUpdate, user: CurrentUser, session: SessionDependency
) -> ReferralCodeRead:
    record = await ReferralCodeService(session).update_for_owner(code_id, user.id, data)
    if record is None:
        raise HTTPException(status_code=404, detail="Referral code not found")
    return ReferralCodeRead.model_validate(record)


@router.post(
    "/track",
    response_model=ReferralClickRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": ReferralClickRead, "description": "Idempotent replay"},
        404: {"description": "Referral code unavailable"},
        409: {"description": "Request ID already used for a different click"},
    },
)
async def track_click(
    data: ReferralClickCreate, session: SessionDependency, response: Response
) -> ReferralClickRead:
    try:
        click, created = await ReferralClickService(session).track(data)
    except ReferralCodeUnavailableError as exc:
        raise HTTPException(status_code=404, detail="Referral code unavailable") from exc
    except ReferralClickConflictError as exc:
        raise HTTPException(
            status_code=409, detail="Request ID already used for a different click"
        ) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Cache-Control"] = "no-store"
    return ReferralClickRead.model_validate(click)
