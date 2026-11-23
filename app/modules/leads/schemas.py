from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from app.common.validation import validate_postgres_text
from app.modules.leads.models import LeadStatus


class LeadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
        AfterValidator(validate_postgres_text),
    ]


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


class LeadPage(BaseModel):
    items: list[LeadRead]
    limit: int
    offset: int


class LeadStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: LeadStatus


class LeadAssignmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manager_id: UUID | None


class LeadStatusHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lead_id: UUID
    previous_status: LeadStatus | None
    new_status: LeadStatus
    changed_by: UUID
    changed_at: datetime


class LeadStatusHistoryPage(BaseModel):
    items: list[LeadStatusHistoryRead]
    limit: int
    offset: int
