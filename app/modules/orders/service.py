from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import Order, OrderStatus
from app.modules.orders.repository import OrderRepository
from app.modules.users.models import User


class OrderService:
    def __init__(self, session: AsyncSession):
        self.orders = OrderRepository(session)

    async def list_visible(
        self, user: User, status: OrderStatus | None, currency: str | None, limit: int, offset: int
    ) -> list[Order]:
        return await self.orders.list_visible(user, status, currency, limit, offset)

    async def get_visible(self, user: User, order_id: UUID) -> Order | None:
        return await self.orders.get_visible(user, order_id)
