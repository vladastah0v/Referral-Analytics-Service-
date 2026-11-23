import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tracking.metrics import DUPLICATE_EVENTS
from app.modules.tracking.models import Event, EventOutbox
from app.modules.tracking.schemas import EventCreate


class EventConflictError(Exception):
    pass


class EventIngestionService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def accept(self, source: str, data: EventCreate) -> tuple[Event, bool]:
        normalized = data.model_dump(mode="json")
        data = data.root
        digest = hashlib.sha256(
            json.dumps(
                normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        try:
            statement = (
                insert(Event)
                .values(
                    source_system=source,
                    external_event_id=data.external_event_id,
                    event_type=data.event_type,
                    occurred_at=data.occurred_at,
                    payload=normalized["payload"],
                    payload_hash=digest,
                )
                .on_conflict_do_nothing(constraint="uq_events_source_external_id")
                .returning(Event)
            )
            event = await self.session.scalar(statement)
            if event is None:
                event = await self.session.scalar(
                    select(Event).where(
                        Event.source_system == source,
                        Event.external_event_id == data.external_event_id,
                    )
                )
                if event is None:
                    raise RuntimeError("Conflicting event could not be loaded")
                if event.payload_hash != digest:
                    # Existing rows may use a different decimal scale in their original fingerprint.
                    previous_payload = event.payload.copy()
                    for field in ("amount", "total_amount"):
                        if field in previous_payload:
                            previous_payload[field] = format(
                                Decimal(previous_payload[field]), ".2f"
                            )
                    if (
                        event.event_type != data.event_type
                        or event.occurred_at != data.occurred_at
                        or previous_payload != normalized["payload"]
                    ):
                        raise EventConflictError
                await self.session.commit()
                DUPLICATE_EVENTS.labels("ingestion").inc()
                return event, False
            self.session.add(EventOutbox(event_id=event.id))
            await self.session.commit()
            return event, True
        except Exception:
            await self.session.rollback()
            raise
