from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import UUID4, AwareDatetime

from app.core.dependencies import CurrentUser, SessionDependency
from app.modules.leads.models import LeadStatus
from app.modules.leads.schemas import LeadCreate, LeadPage, LeadRead, LeadStatusUpdate
from app.modules.leads.service import (
    InvalidLeadRangeError,
    InvalidLeadTransitionError,
    LeadCreationConflictError,
    LeadService,
)

router = APIRouter(prefix="/leads", tags=["leads"])


@router.post(
    "",
    response_model=LeadRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": LeadRead, "description": "Idempotent replay"},
        409: {"description": "Idempotency key already used with different data"},
    },
)
async def create_lead(
    data: LeadCreate,
    user: CurrentUser,
    session: SessionDependency,
    response: Response,
    idempotency_key: Annotated[UUID4 | None, Header(alias="Idempotency-Key")] = None,
) -> LeadRead:
    try:
        lead, created = await LeadService(session).create(user.id, data, idempotency_key)
    except LeadCreationConflictError as exc:
        raise HTTPException(
            status_code=409, detail="Idempotency key already used with different data"
        ) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Cache-Control"] = "private, no-store"
    return LeadRead.model_validate(lead)


@router.get("", response_model=LeadPage)
async def list_leads(
    user: CurrentUser,
    session: SessionDependency,
    response: Response,
    status: LeadStatus | None = None,
    manager_id: UUID | None = None,
    date_from: AwareDatetime | None = None,
    date_to: AwareDatetime | None = None,
    sort: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2147483647)] = 0,
) -> LeadPage:
    try:
        records = await LeadService(session).list_visible(
            user, status, manager_id, date_from, date_to, sort, limit, offset
        )
    except InvalidLeadRangeError as exc:
        raise HTTPException(
            status_code=422, detail="date_from must be earlier than date_to"
        ) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return LeadPage(
        items=[LeadRead.model_validate(record) for record in records], limit=limit, offset=offset
    )


@router.get("/{lead_id}", response_model=LeadRead)
async def get_lead(
    lead_id: UUID, user: CurrentUser, session: SessionDependency, response: Response
) -> LeadRead:
    lead = await LeadService(session).get_visible(lead_id, user)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    response.headers["Cache-Control"] = "private, no-store"
    return LeadRead.model_validate(lead)


@router.patch("/{lead_id}/status", response_model=LeadRead)
async def update_status(
    lead_id: UUID,
    data: LeadStatusUpdate,
    user: CurrentUser,
    session: SessionDependency,
    response: Response,
) -> LeadRead:
    try:
        lead = await LeadService(session).change_status(lead_id, user, data.status)
    except InvalidLeadTransitionError as exc:
        raise HTTPException(status_code=409, detail="Invalid status transition") from exc
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    response.headers["Cache-Control"] = "private, no-store"
    return LeadRead.model_validate(lead)
