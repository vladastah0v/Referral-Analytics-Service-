from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leads.models import Lead, LeadStatus, LeadStatusHistory
from app.modules.users.models import User, UserRole


def lead_visibility(user: User):
    if user.role == UserRole.ADMIN:
        return true()
    if user.role == UserRole.MANAGER:
        return Lead.assigned_manager_id == user.id
    return Lead.user_id == user.id


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

    async def get_visible(self, lead_id, user):
        return await self.session.scalar(
            select(Lead).where(Lead.id == lead_id, lead_visibility(user))
        )

    async def list_visible(self, user, status, manager_id, date_from, date_to, sort, limit, offset):
        statement = select(Lead).where(lead_visibility(user))
        if status is not None:
            statement = statement.where(Lead.status == status)
        if manager_id is not None:
            statement = statement.where(Lead.assigned_manager_id == manager_id)
        if date_from is not None:
            statement = statement.where(Lead.created_at >= date_from)
        if date_to is not None:
            statement = statement.where(Lead.created_at < date_to)
        order = (
            (Lead.created_at.asc(), Lead.id.asc())
            if sort == "asc"
            else (Lead.created_at.desc(), Lead.id.desc())
        )
        return list(
            (
                await self.session.scalars(statement.order_by(*order).limit(limit).offset(offset))
            ).all()
        )

    async def lock_for_status_update(self, lead_id, user):
        if user.role == UserRole.CUSTOMER:
            return None
        return await self.session.scalar(
            select(Lead).where(Lead.id == lead_id, lead_visibility(user)).with_for_update()
        )

    def add_status_history(self, lead_id, user_id, previous, new, changed_at):
        self.session.add(
            LeadStatusHistory(
                lead_id=lead_id,
                changed_by=user_id,
                previous_status=previous,
                new_status=new,
                changed_at=changed_at,
            )
        )

    async def lock_for_assignment(self, lead_id):
        return await self.session.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())

    async def lock_active_manager(self, manager_id):
        return await self.session.scalar(
            select(User)
            .where(User.id == manager_id, User.role == UserRole.MANAGER, User.is_active.is_(True))
            .with_for_update(read=True)
        )

    async def lock_visible_for_history(self, lead_id: UUID, user: User) -> UUID | None:
        statement = (
            select(Lead.id)
            .where(Lead.id == lead_id, lead_visibility(user))
            .with_for_update(read=True)
        )
        return await self.session.scalar(statement)

    async def list_status_history(
        self, lead_id: UUID, limit: int, offset: int
    ) -> list[LeadStatusHistory]:
        statement = (
            select(LeadStatusHistory)
            .where(LeadStatusHistory.lead_id == lead_id)
            .order_by(LeadStatusHistory.changed_at.asc(), LeadStatusHistory.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.scalars(statement)).all())
