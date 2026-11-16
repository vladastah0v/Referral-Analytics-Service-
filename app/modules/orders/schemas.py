from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.orders.models import OrderStatus


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    external_order_id: str
    source_system: str
    user_id: UUID
    referral_attribution_id: UUID | None
    attribution_frozen_at: datetime | None
    status: OrderStatus
    total_amount: Decimal
    currency: str
    paid_at: datetime | None
    external_created_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OrderPage(BaseModel):
    items: list[OrderRead]
    limit: int
    offset: int
