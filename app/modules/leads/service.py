import hashlib
import hmac
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leads.models import Lead, LeadStatus
from app.modules.leads.repository import LeadRepository
from app.modules.leads.schemas import LeadCreate
from app.modules.referrals.repository import ReferralAttributionRepository


class LeadCreationConflictError(Exception):
    pass


class InvalidLeadRangeError(Exception):
    pass


class InvalidLeadTransitionError(Exception):
    pass


class InvalidManagerError(Exception):
    pass


class LeadService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.leads = LeadRepository(session)
        self.attributions = ReferralAttributionRepository(session)

    @staticmethod
    def _request_hash(data: LeadCreate) -> str:
        # Update the versioned fingerprint when the creation payload changes.
        return hashlib.sha256(("lead:create:v1\0" + data.source).encode("utf-8")).hexdigest()

    @staticmethod
    def _check_replay(lead: Lead, request_hash: str) -> Lead:
        if lead.creation_request_hash is None or not hmac.compare_digest(
            lead.creation_request_hash, request_hash
        ):
            raise LeadCreationConflictError
        return lead

    async def create(
        self, user_id: UUID, data: LeadCreate, request_id: UUID | None
    ) -> tuple[Lead, bool]:
        request_hash = self._request_hash(data) if request_id is not None else None
        try:
            # Shared with registration attribution to fix the lead's source consistently.
            await self.attributions.lock_user(user_id)
            if request_id is not None:
                existing = await self.leads.get_by_creation_request(user_id, request_id)
                if existing is not None:
                    result = self._check_replay(existing, request_hash)
                    await self.session.commit()
                    return result, False
            attribution = await self.attributions.get_for_user(user_id)
            lead = await self.leads.create_if_new(
                user_id,
                attribution.id if attribution else None,
                data.source,
                request_id,
                request_hash,
            )
            if lead is None:
                if request_id is None or request_hash is None:
                    raise RuntimeError("Unexpected lead creation conflict")
                existing = await self.leads.get_by_creation_request(user_id, request_id)
                if existing is None:
                    raise RuntimeError("Conflicting lead request could not be loaded")
                result = self._check_replay(existing, request_hash)
                await self.session.commit()
                return result, False
            self.leads.add_initial_history(lead.id, user_id)
            await self.session.commit()
            return lead, True
        except Exception:
            await self.session.rollback()
            raise

    async def get_visible(self, lead_id, user):
        return await self.leads.get_visible(lead_id, user)

    async def list_visible(self, user, status, manager_id, date_from, date_to, sort, limit, offset):
        if date_from is not None and date_to is not None and date_from >= date_to:
            raise InvalidLeadRangeError
        return await self.leads.list_visible(
            user, status, manager_id, date_from, date_to, sort, limit, offset
        )

    async def change_status(self, lead_id, user, new_status):
        transitions = {
            LeadStatus.NEW: {LeadStatus.CONTACTED, LeadStatus.LOST},
            LeadStatus.CONTACTED: {LeadStatus.QUALIFIED, LeadStatus.LOST},
            LeadStatus.QUALIFIED: {LeadStatus.CONVERTED, LeadStatus.LOST},
            LeadStatus.CONVERTED: set(),
            LeadStatus.LOST: set(),
        }
        try:
            lead = await self.leads.lock_for_status_update(lead_id, user)
            if lead is None:
                return None
            previous = lead.status
            if new_status not in transitions[previous]:
                raise InvalidLeadTransitionError
            now = await self.session.scalar(select(func.clock_timestamp()))
            lead.status = new_status
            lead.updated_at = now
            if new_status == LeadStatus.QUALIFIED:
                lead.qualified_at = now
            if new_status == LeadStatus.CONVERTED:
                lead.converted_at = now
            self.leads.add_status_history(lead.id, user.id, previous, new_status, now)
            await self.session.flush()
            await self.session.refresh(lead)
            await self.session.commit()
            return lead
        except Exception:
            await self.session.rollback()
            raise

    async def assign(self, lead_id, manager_id):
        try:
            lead = await self.leads.lock_for_assignment(lead_id)
            if lead is None:
                return None
            if manager_id is not None and await self.leads.lock_active_manager(manager_id) is None:
                raise InvalidManagerError
            if lead.assigned_manager_id != manager_id:
                lead.assigned_manager_id = manager_id
                lead.updated_at = await self.session.scalar(select(func.clock_timestamp()))
                await self.session.flush()
                await self.session.refresh(lead)
            await self.session.commit()
            return lead
        except Exception:
            await self.session.rollback()
            raise
