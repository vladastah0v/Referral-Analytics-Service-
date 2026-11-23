from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SkipValidation,
    StringConstraints,
    field_serializer,
    field_validator,
)

from app.common.validation import UTCDateTime, validate_postgres_text

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=180, pattern=r"^[^\x00-\x1f\x7f]+$"
    ),
    AfterValidator(validate_postgres_text),
]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Amount = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]


class CreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_order_id: Identifier
    user_id: UUID
    total_amount: Amount
    currency: Currency

    @field_validator("total_amount", mode="before", json_schema_input_type=str)
    @classmethod
    def money_is_string(cls, value):
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError("Amount must be a decimal string")
        return value

    @field_serializer("total_amount", when_used="json")
    def serialize_amount(self, value: Decimal) -> str:
        return format(value, ".2f")


class FinancialPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_order_id: Identifier
    operation_id: Identifier
    amount: Amount
    currency: Currency

    @field_validator("amount", mode="before", json_schema_input_type=str)
    @classmethod
    def money_is_string(cls, value):
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError("Amount must be a decimal string")
        return value

    @field_serializer("amount", when_used="json")
    def serialize_amount(self, value: Decimal) -> str:
        return format(value, ".2f")


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


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_event_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$"
        ),
        AfterValidator(validate_postgres_text),
    ]
    occurred_at: UTCDateTime

    @field_validator("occurred_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value < datetime(2000, 1, 1, tzinfo=UTC) or value > datetime.now(UTC) + timedelta(
            minutes=5
        ):
            raise ValueError("Event time outside accepted range")
        return value


class OrderCreatedEvent(EventEnvelope):
    event_type: Literal["order_created"]
    payload: CreatedPayload


class OrderFinancialEvent(EventEnvelope):
    event_type: Literal["order_paid", "order_refunded"]
    payload: FinancialPayload


class OrderAdjustedEvent(EventEnvelope):
    event_type: Literal["order_adjusted"]
    payload: AdjustmentPayload


class OrderCancelledEvent(EventEnvelope):
    event_type: Literal["order_cancelled"]
    payload: CancelledPayload


class ReferralClickedEvent(EventEnvelope):
    event_type: Literal["referral_clicked"]
    payload: ClickFact


class UserRegisteredEvent(EventEnvelope):
    event_type: Literal["user_registered"]
    payload: RegistrationFact


class LeadEvent(EventEnvelope):
    event_type: Literal["lead_created", "lead_qualified"]
    payload: LeadFact


EventInput = Annotated[
    OrderCreatedEvent
    | OrderFinancialEvent
    | OrderAdjustedEvent
    | OrderCancelledEvent
    | ReferralClickedEvent
    | UserRegisteredEvent
    | LeadEvent,
    Field(discriminator="event_type"),
]


class EventCreate(RootModel[EventInput]):
    pass


class EventReceipt(BaseModel):
    id: UUID
    processing_status: str
    replayed: bool


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: Annotated[list[SkipValidation[EventInput]], Field(min_length=1, max_length=100)]


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
