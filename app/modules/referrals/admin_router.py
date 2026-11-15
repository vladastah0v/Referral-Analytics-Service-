from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.dependencies import SessionDependency, require_roles
from app.modules.referrals.schemas import (
    ReferralProgramCreate,
    ReferralProgramPage,
    ReferralProgramRead,
    ReferralProgramUpdate,
)
from app.modules.referrals.service import InvalidProgramUpdateError, ReferralProgramService
from app.modules.users.models import UserRole

router = APIRouter(
    prefix="/admin/referral-programs",
    tags=["admin referral programs"],
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)


@router.post("", response_model=ReferralProgramRead, status_code=status.HTTP_201_CREATED)
async def create_program(
    data: ReferralProgramCreate, session: SessionDependency
) -> ReferralProgramRead:
    return ReferralProgramRead.model_validate(await ReferralProgramService(session).create(data))


@router.get("", response_model=ReferralProgramPage)
async def list_programs(
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
) -> ReferralProgramPage:
    records = await ReferralProgramService(session).list(limit, offset)
    return ReferralProgramPage(
        items=[ReferralProgramRead.model_validate(record) for record in records],
        limit=limit,
        offset=offset,
    )


@router.patch("/{program_id}", response_model=ReferralProgramRead)
async def update_program(
    program_id: UUID, data: ReferralProgramUpdate, session: SessionDependency
) -> ReferralProgramRead:
    try:
        program = await ReferralProgramService(session).update(program_id, data)
    except InvalidProgramUpdateError as exc:
        raise HTTPException(status_code=422, detail="Invalid program configuration") from exc
    if program is None:
        raise HTTPException(status_code=404, detail="Referral program not found")
    return ReferralProgramRead.model_validate(program)
