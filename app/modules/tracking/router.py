from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy import String, and_, exists, or_, select

from app.core.dependencies import CurrentUser, SessionDependency
from app.modules.leads.models import Lead
from app.modules.orders.models import Order
from app.modules.tracking.metrics import EVENT_INGESTION_FAILURES
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
        EVENT_INGESTION_FAILURES.labels("single", "409").inc()
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
            EVENT_INGESTION_FAILURES.labels("batch", "422").inc()
            results.append(BatchItem(index=index, status_code=422, error="Invalid event"))
            continue
        try:
            event, created = await EventIngestionService(session).accept(source_system, event_data)
        except EventConflictError:
            EVENT_INGESTION_FAILURES.labels("batch", "409").inc()
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
    statement = (
        select(Event, EventOutbox.attempts, EventOutbox.last_error)
        .join(EventOutbox, EventOutbox.event_id == Event.id)
        .where(Event.id == event_id)
    )
    if user.role == UserRole.MANAGER:
        assigned_user = exists().where(
            Lead.user_id == Event.user_id, Lead.assigned_manager_id == user.id
        )
        assigned_lead = exists().where(
            Lead.id.cast(String) == Event.payload["lead_id"].as_string(),
            Lead.user_id == Event.user_id,
            Lead.assigned_manager_id == user.id,
        )
        confirmed_order = exists().where(
            Order.source_system == Event.source_system,
            Order.external_order_id == Event.payload["external_order_id"].as_string(),
            Order.user_id == Event.user_id,
        )
        statement = statement.where(
            Event.processing_status == "processed",
            or_(
                and_(Event.event_type.in_(("lead_created", "lead_qualified")), assigned_lead),
                and_(Event.event_type == "user_registered", assigned_user),
                and_(Event.event_type.startswith("order_"), assigned_user, confirmed_order),
            ),
        )
    result = (await session.execute(statement)).one_or_none()
    if result is None:
        raise HTTPException(status_code=404, detail="Event not found")
    event, attempts, last_error = result
    response.headers["Cache-Control"] = "private, no-store"
    return EventRead.model_validate(event).model_copy(
        update={"attempts": attempts, "last_error": last_error}
    )
