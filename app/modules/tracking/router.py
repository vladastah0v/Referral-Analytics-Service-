from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy import exists, select

from app.core.dependencies import CurrentUser, SessionDependency
from app.modules.leads.models import Lead
from app.modules.orders.models import Order
from app.modules.tracking.models import Event, EventOutbox
from app.modules.tracking.schemas import (
    BatchItem,
    BatchRequest,
    BatchResponse,
    EventCreate,
    EventRead,
    EventReceipt,
)
from app.modules.tracking.security import IntegrationPrincipal
from app.modules.tracking.service import EventConflictError, EventIngestionService
from app.modules.users.models import UserRole

router = APIRouter(prefix="/events", tags=["events"])


@router.post(
    "",
    response_model=EventReceipt,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"model": EventReceipt}, 409: {"description": "Event ID conflict"}},
)
async def ingest_event(
    data: EventCreate,
    principal: IntegrationPrincipal,
    session: SessionDependency,
    response: Response,
) -> EventReceipt:
    try:
        event, created = await EventIngestionService(session).accept(principal.source_system, data)
    except EventConflictError as exc:
        raise HTTPException(
            status_code=409, detail="Event ID already used with different data"
        ) from exc
    if not created:
        response.status_code = 200
    response.headers["Cache-Control"] = "no-store"
    return EventReceipt(
        id=event.id, processing_status=event.processing_status, replayed=not created
    )


@router.post("/batch", response_model=BatchResponse, status_code=207)
async def ingest_batch(
    data: BatchRequest,
    principal: IntegrationPrincipal,
    session: SessionDependency,
    response: Response,
) -> BatchResponse:
    results = []
    source_system = principal.source_system
    for index, raw in enumerate(data.events):
        try:
            event_data = EventCreate.model_validate(raw)
        except ValidationError:
            results.append(BatchItem(index=index, status_code=422, error="Invalid event"))
            continue
        try:
            event, created = await EventIngestionService(session).accept(source_system, event_data)
        except EventConflictError:
            results.append(BatchItem(index=index, status_code=409, error="Event ID conflict"))
            continue
        results.append(
            BatchItem(
                index=index,
                status_code=202 if created else 200,
                event=EventReceipt(
                    id=event.id, processing_status=event.processing_status, replayed=not created
                ),
            )
        )
    response.headers["Cache-Control"] = "no-store"
    return BatchResponse(results=results)


@router.get("/{event_id}", response_model=EventRead)
async def get_event(
    event_id: UUID, user: CurrentUser, session: SessionDependency, response: Response
) -> EventRead:
    if user.role not in (UserRole.MANAGER, UserRole.ADMIN):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    if user.role == UserRole.MANAGER:
        if event.event_type in ("lead_created", "lead_qualified"):
            visible = event.processing_status == "processed" and await session.scalar(
                select(
                    exists().where(
                        Lead.id == UUID(event.payload["lead_id"]),
                        Lead.assigned_manager_id == user.id,
                    )
                )
            )
        elif event.event_type == "user_registered":
            visible = event.processing_status == "processed" and await session.scalar(
                select(
                    exists().where(
                        Lead.user_id == event.user_id, Lead.assigned_manager_id == user.id
                    )
                )
            )
        elif event.event_type == "referral_clicked":
            visible = False
        else:
            order_id = event.payload.get("external_order_id")
            visible = await session.scalar(
                select(
                    exists().where(
                        Order.source_system == event.source_system,
                        Order.external_order_id == order_id,
                        exists().where(
                            Lead.user_id == Order.user_id, Lead.assigned_manager_id == user.id
                        ),
                    )
                )
            )
        if not visible:
            raise HTTPException(status_code=404, detail="Event not found")
    outbox = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == event.id))
    if outbox is None:
        raise RuntimeError("Outbox event missing")
    response.headers["Cache-Control"] = "private, no-store"
    return EventRead.model_validate(event).model_copy(
        update={"attempts": outbox.attempts, "last_error": outbox.last_error}
    )
