from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=180, pattern=r"^[^\x00-\x1f\x7f]+$"
    ),
]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Amount = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]
OrderAmount = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]


class CreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_order_id: Identifier
    user_id: UUID
    total_amount: OrderAmount
    currency: Currency

    @field_validator("total_amount", mode="before")
    @classmethod
    def money_is_string(cls, value):
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError("Amount must be a decimal string")
        return value


class FinancialPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_order_id: Identifier
    operation_id: Identifier
    amount: Amount
    currency: Currency

    @field_validator("amount", mode="before")
    @classmethod
    def money_is_string(cls, value):
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError("Amount must be a decimal string")
        return value


class AdjustmentPayload(FinancialPayload):
    direction: Literal["credit", "debit"]


class CancelledPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_order_id: Identifier


class ClickFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    click_id: UUID


class RegistrationFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID


class LeadFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: UUID


EventType = Literal[
    "order_created",
    "order_paid",
    "order_cancelled",
    "order_refunded",
    "order_adjusted",
    "referral_clicked",
    "user_registered",
    "lead_created",
    "lead_qualified",
]


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_event_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$"
        ),
    ]
    event_type: EventType

    occurred_at: AwareDatetime
    payload: dict[str, Any]

    @field_validator("occurred_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        value = value.astimezone(UTC)
        if value < datetime(2000, 1, 1, tzinfo=UTC) or value > datetime.now(UTC) + timedelta(
            minutes=5
        ):
            raise ValueError("Event time outside accepted range")
        return value

    @model_validator(mode="after")
    def validate_payload(self):
        payload_type = {
            "referral_clicked": ClickFact,
            "user_registered": RegistrationFact,
            "lead_created": LeadFact,
            "lead_qualified": LeadFact,
            "order_created": CreatedPayload,
            "order_paid": FinancialPayload,
            "order_refunded": FinancialPayload,
            "order_adjusted": AdjustmentPayload,
            "order_cancelled": CancelledPayload,
        }[self.event_type]
        try:
            self.payload = payload_type.model_validate(self.payload).model_dump(mode="json")
        except ValidationError as exc:
            raise ValueError("Invalid event payload") from exc
        return self


class EventReceipt(BaseModel):
    id: UUID
    processing_status: str
    replayed: bool


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: Annotated[list[Any], Field(min_length=1, max_length=100)]


class BatchItem(BaseModel):
    index: int
    status_code: int
    event: EventReceipt | None = None
    error: str | None = None


class BatchResponse(BaseModel):
    results: list[BatchItem]


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    external_event_id: str
    source_system: str
    user_id: UUID | None
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    received_at: datetime
    processing_status: str
    error_code: str | None
    attempts: int = 0
    last_error: str | None = None
