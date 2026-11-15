from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leads.models import Lead, LeadStatus, LeadStatusHistory


class LeadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_creation_request(self, user_id: UUID, request_id: UUID) -> Lead | None:
        statement = select(Lead).where(
            Lead.user_id == user_id, Lead.creation_request_id == request_id
        )
        return await self.session.scalar(statement)

    async def create_if_new(
        self,
        user_id: UUID,
        attribution_id: UUID | None,
        source: str,
        request_id: UUID | None,
        request_hash: str | None,
    ) -> Lead | None:
        statement = (
            insert(Lead)
            .values(
                user_id=user_id,
                referral_attribution_id=attribution_id,
                status=LeadStatus.NEW,
                source=source,
                creation_request_id=request_id,
                creation_request_hash=request_hash,
            )
            .on_conflict_do_nothing(constraint="uq_leads_user_creation_request")
            .returning(Lead)
        )
        return await self.session.scalar(statement)

    def add_initial_history(self, lead_id: UUID, user_id: UUID) -> None:
        self.session.add(
            LeadStatusHistory(
                lead_id=lead_id,
                previous_status=None,
                new_status=LeadStatus.NEW,
                changed_by=user_id,
            )
        )
