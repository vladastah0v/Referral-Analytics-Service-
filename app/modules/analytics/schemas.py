from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

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


class RevenuePeriod(BaseModel):
    period_start: datetime
    items: list[CurrencyRevenue]


class RevenueReport(BaseModel):
    group_by: Grouping | None
    periods: list[RevenuePeriod]
    date_from: datetime | None
    date_to: datetime | None
    items: list[CurrencyRevenue]


class ReferrerRevenue(BaseModel):
    position: int
    referrer_id: UUID
    currency: str
    paid_orders: int
    net_revenue: Decimal

    @field_serializer("net_revenue")
    def serialize_net(self, value: Decimal) -> str:
        return format(value, ".2f")


class ReferrerRankingPeriod(BaseModel):
    period_start: datetime
    items: list[ReferrerRevenue]


class ReferrerRanking(BaseModel):
    date_from: datetime | None
    date_to: datetime | None
    group_by: Grouping | None
    limit: int
    items: list[ReferrerRevenue]
    periods: list[ReferrerRankingPeriod]
