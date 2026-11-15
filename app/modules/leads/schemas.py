from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.modules.leads.models import LeadStatus


class LeadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    referral_attribution_id: UUID | None
    status: LeadStatus
    assigned_manager_id: UUID | None
    source: str
    created_at: datetime
    updated_at: datetime
    qualified_at: datetime | None
    converted_at: datetime | None
