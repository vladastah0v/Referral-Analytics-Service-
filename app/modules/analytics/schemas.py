from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, field_serializer

Grouping = Literal["day", "week", "month"]


class CurrencyRevenue(BaseModel):
    currency: str
    paid_orders: int
    gross_revenue: Decimal
    refunds: Decimal
    adjustments_debit: Decimal
    net_revenue: Decimal
    referral_attributed_revenue: Decimal

    @field_serializer(
        "gross_revenue",
        "refunds",
        "adjustments_debit",
        "net_revenue",
        "referral_attributed_revenue",
    )
    def serialize_amount(self, value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01")), ".2f")


class RevenueReport(BaseModel):
    date_from: datetime | None
    date_to: datetime | None
    items: list[CurrencyRevenue]
