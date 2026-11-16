from uuid import UUID

from sqlalchemy import exists, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leads.models import Lead
from app.modules.orders.models import Order, OrderStatus
from app.modules.users.models import User, UserRole


def order_visibility(user: User):
    if user.role == UserRole.ADMIN:
        return true()
    if user.role == UserRole.MANAGER:
        return exists().where(Lead.user_id == Order.user_id, Lead.assigned_manager_id == user.id)
    return Order.user_id == user.id


class OrderRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_visible(
        self, user: User, status: OrderStatus | None, currency: str | None, limit: int, offset: int
    ) -> list[Order]:
        statement = select(Order).where(order_visibility(user))
        if status is not None:
            statement = statement.where(Order.status == status)
        if currency is not None:
            statement = statement.where(Order.currency == currency)
        statement = (
            statement.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).offset(offset)
        )
        return list((await self.session.scalars(statement)).all())

    async def get_visible(self, user: User, order_id: UUID) -> Order | None:
        return await self.session.scalar(
            select(Order).where(Order.id == order_id, order_visibility(user))
        )
