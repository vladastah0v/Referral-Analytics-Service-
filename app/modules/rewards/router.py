from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.dependencies import CurrentUser, SessionDependency, require_roles
from app.modules.rewards.models import RewardStatus
from app.modules.rewards.repository import list_rewards
from app.modules.rewards.schemas import (
    AdminRewardPage,
    AdminRewardRead,
    RewardApprove,
    RewardPage,
)
from app.modules.rewards.service import RewardApprovalConflict, approve_reward
from app.modules.users.models import User, UserRole

router = APIRouter(tags=["rewards"])
AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
Currency = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=2147483647)]


@router.get("/rewards/me", response_model=RewardPage)
async def own_rewards(
    user: CurrentUser,
    session: SessionDependency,
    response: Response,
    status: RewardStatus | None = None,
    currency: Currency = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> RewardPage:
    records = await list_rewards(session, user.id, status, currency, limit, offset)
    response.headers["Cache-Control"] = "private, no-store"
    return RewardPage(items=records, limit=limit, offset=offset)


@router.get("/admin/rewards", response_model=AdminRewardPage)
async def admin_rewards(
    user: AdminUser,
    session: SessionDependency,
    response: Response,
    status: RewardStatus | None = None,
    currency: Currency = None,
    referrer_id: UUID | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> AdminRewardPage:
    records = await list_rewards(session, referrer_id, status, currency, limit, offset)
    response.headers["Cache-Control"] = "private, no-store"
    return AdminRewardPage(items=records, limit=limit, offset=offset)


@router.patch("/admin/rewards/{reward_id}/approve", response_model=AdminRewardRead)
async def approve(
    reward_id: UUID,
    data: RewardApprove,
    user: AdminUser,
    session: SessionDependency,
    response: Response,
) -> AdminRewardRead:
    try:
        record = await approve_reward(session, reward_id, user.id, data.expected_revision)
    except RewardApprovalConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Reward changed or cannot be approved",
            headers={"Cache-Control": "no-store"},
        ) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Reward not found")
    response.headers["Cache-Control"] = "private, no-store"
    return AdminRewardRead.model_validate(record)
