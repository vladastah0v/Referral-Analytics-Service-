from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response

from app.core.dependencies import CurrentUser, SessionDependency
from app.modules.orders.models import OrderStatus
from app.modules.orders.schemas import OrderPage, OrderRead
from app.modules.orders.service import OrderService

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=OrderPage)
async def list_orders(
    user: CurrentUser,
    session: SessionDependency,
    response: Response,
    status: OrderStatus | None = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2147483647)] = 0,
) -> OrderPage:
    orders = await OrderService(session).list_visible(user, status, currency, limit, offset)
    response.headers["Cache-Control"] = "private, no-store"
    return OrderPage(
        items=[OrderRead.model_validate(order) for order in orders], limit=limit, offset=offset
    )


@router.get("/{order_id}", response_model=OrderRead)
async def get_order(
    order_id: UUID, user: CurrentUser, session: SessionDependency, response: Response
) -> OrderRead:
    order = await OrderService(session).get_visible(user, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    response.headers["Cache-Control"] = "private, no-store"
    return OrderRead.model_validate(order)
