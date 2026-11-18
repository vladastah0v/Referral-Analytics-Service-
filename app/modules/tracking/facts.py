from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leads.models import Lead
from app.modules.orders.processor import DeferredOrderEvent, RejectedOrderEvent
from app.modules.referrals.models import ReferralClick
from app.modules.tracking.models import Event
from app.modules.users.models import User


async def confirm_fact(session: AsyncSession, event: Event) -> None:
    """Resolve notifications against the authoritative registration, tracking and CRM records."""
    if event.event_type == "referral_clicked":
        click = await session.get(ReferralClick, UUID(event.payload["click_id"]))
        occurred = click.clicked_at if click else None
    elif event.event_type == "user_registered":
        user = await session.get(User, UUID(event.payload["user_id"]))
        occurred = user.created_at if user else None
        event.user_id = user.id if user else None
    elif event.event_type in ("lead_created", "lead_qualified"):
        lead = await session.get(Lead, UUID(event.payload["lead_id"]))
        occurred = None
        if lead:
            event.user_id = lead.user_id
            occurred = lead.created_at if event.event_type == "lead_created" else lead.qualified_at
    else:
        raise RejectedOrderEvent("unknown_event_type")
    if occurred is None:
        raise DeferredOrderEvent("fact_missing")
    if event.occurred_at != occurred:
        raise RejectedOrderEvent("fact_time_conflict")
