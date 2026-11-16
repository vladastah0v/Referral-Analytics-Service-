from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import AwareDatetime

from app.core.dependencies import CurrentUser, SessionDependency
from app.modules.analytics.schemas import RevenueReport
from app.modules.analytics.service import InvalidRevenueRangeError, RevenueService
from app.modules.users.models import UserRole

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/revenue", response_model=RevenueReport)
async def revenue(
    user: CurrentUser,
    session: SessionDependency,
    response: Response,
    date_from: AwareDatetime | None = None,
    date_to: AwareDatetime | None = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
) -> RevenueReport:
    if user.role not in (UserRole.MANAGER, UserRole.ADMIN):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        report = await RevenueService(session).report(user, date_from, date_to, currency)
    except InvalidRevenueRangeError as exc:
        raise HTTPException(
            status_code=422, detail="date_from must be earlier than date_to"
        ) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return report
