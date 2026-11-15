from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import UUID4

from app.core.dependencies import CurrentUser, SessionDependency
from app.modules.leads.schemas import LeadCreate, LeadRead
from app.modules.leads.service import LeadCreationConflictError, LeadService

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
