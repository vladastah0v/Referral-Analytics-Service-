"""Send synthetic order payments and refunds through the tracking API."""

import argparse
import asyncio
import json
import os
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import batched

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine, session_factory
from app.modules.orders.models import Order, OrderFinancialEvent
from app.modules.referrals.models import ReferralProgram
from app.modules.rewards.models import ReferralReward
from app.modules.tracking.models import Event
from app.modules.tracking.schemas import BatchResponse
from app.modules.tracking.security import authenticate_integration

CENT = Decimal("0.01")


async def load_orders(session: AsyncSession, seed: int) -> list[Order]:
    program = await session.scalar(
        select(ReferralProgram.id).where(ReferralProgram.name == f"Synthetic {seed}").limit(1)
    )
    if program is None:
        raise ValueError(f"Набор seed={seed} не найден. Сначала выполните scripts.seed.")
    return list(
        await session.scalars(
            select(Order).where(Order.source_system == f"synthetic-{seed}").order_by(Order.id)
        )
    )


def refund_amount(order: Order) -> Decimal:
    bucket = order.id.int % 10
    if bucket == 0:
        return order.total_amount
    if bucket == 1:
        return (order.total_amount / 2).quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal("0.00")


def order_events(order: Order) -> Iterator[dict]:
    refund = refund_amount(order)
    for kind, amount, delay in (
        ("order_paid", order.total_amount, 1),
        ("order_refunded", refund, 2),
    ):
        if not amount:
            continue
        event_id = f"{order.id}:{kind}"
        yield {
            "external_event_id": event_id,
            "event_type": kind,
            "occurred_at": (order.external_created_at + timedelta(seconds=delay)).isoformat(),
            "payload": {
                "external_order_id": order.external_order_id,
                "operation_id": event_id,
                "currency": order.currency,
                "amount": format(amount, ".2f"),
            },
        }


async def send_events(
    factory, client: httpx.AsyncClient, key: str, seed: int, batch_size: int
) -> dict:
    if not 1 <= batch_size <= 100:
        raise ValueError("Размер пакета должен быть от 1 до 100.")
    async with factory() as session:
        try:
            credential = await authenticate_integration(session, key)
        except HTTPException as exc:
            raise ValueError("Недействительный ключ интеграции.") from exc
        if credential.source_system != f"synthetic-{seed}":
            raise ValueError(f"Нужен ключ источника synthetic-{seed}.")
        orders = await load_orders(session, seed)
    messages = (event for order in orders for event in order_events(order))
    accepted = replayed = 0
    for batch in batched(messages, batch_size):
        response = await client.post(
            "/api/v1/events/batch", headers={"X-Integration-Key": key}, json={"events": batch}
        )
        response.raise_for_status()
        if response.status_code != 207:
            raise ValueError(f"Неожиданный HTTP-статус пакета: {response.status_code}.")
        results = BatchResponse.model_validate(response.json()).results
        if len(results) != len(batch):
            raise ValueError("API вернул неполный результат пакета.")
        for index, item in enumerate(results):
            if item.index != index or item.status_code not in (200, 202) or item.event is None:
                raise ValueError(
                    f"Событие {batch[index]['external_event_id']}: HTTP {item.status_code}."
                )
            if item.event.processing_status == "rejected":
                raise ValueError(f"Событие {item.event.id} уже отклонено обработчиком.")
            if item.status_code == 202:
                accepted += 1
            else:
                replayed += 1
    return {"orders": len(orders), "accepted": accepted, "replayed": replayed}


async def verify_events(session: AsyncSession, seed: int) -> dict:
    # Compare all tables at the same committed point while Celery may still be working.
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    orders = await load_orders(session, seed)
    source = f"synthetic-{seed}"
    statuses = dict(
        (
            await session.execute(
                select(Event.external_event_id, Event.processing_status).where(
                    Event.source_system == source
                )
            )
        ).all()
    )
    ledger = defaultdict(dict)
    for op in await session.scalars(
        select(OrderFinancialEvent)
        .join(Order, Order.id == OrderFinancialEvent.order_id)
        .where(Order.source_system == source)
    ):
        ledger[op.order_id][op.external_financial_event_id] = op
    rewards = dict(
        (
            await session.execute(
                select(ReferralReward.order_id, ReferralReward.reward_amount)
                .join(Order, Order.id == ReferralReward.order_id)
                .where(Order.source_system == source)
            )
        ).all()
    )

    gross = refunds = attributed = accrued = Decimal("0.00")
    for order in orders:
        events = list(order_events(order))
        operations = ledger[order.id]
        for event in events:
            eid = event["external_event_id"]
            state = statuses.get(eid, "missing")
            if state != "processed":
                raise ValueError(f"Событие {eid}: {state}. Проверьте очередь outbox.")
            kind = "payment" if event["event_type"] == "order_paid" else "refund"
            op = operations.get(f"{kind}:{eid}")
            if (
                op is None
                or op.event_type != kind
                or op.source_system != source
                or op.currency != order.currency
                or op.amount != Decimal(event["payload"]["amount"])
                or op.occurred_at != datetime.fromisoformat(event["occurred_at"])
            ):
                raise ValueError(f"Финансовая операция не совпала с событием {eid}.")
            if kind == "payment":
                gross += op.amount
            else:
                refunds += op.amount
        if len(operations) != len(events):
            raise ValueError(f"Лишние финансовые операции заказа {order.external_order_id}.")
        balance = order.total_amount - refund_amount(order)
        expected_reward = None
        if order.referral_attribution_id is not None:
            attributed += balance
            expected_reward = (balance * Decimal("0.05")).quantize(CENT, rounding=ROUND_HALF_UP)
        if rewards.get(order.id) != expected_reward:
            raise ValueError(f"Расхождение начисления по заказу {order.external_order_id}.")
        if expected_reward is not None:
            accrued += rewards[order.id]
    return dict(
        orders=len(orders),
        currency="KZT",
        gross_revenue=gross,
        refunds=refunds,
        net_revenue=gross - refunds,
        referral_attributed_revenue=attributed,
        reward_amount=accrued,
    )


def main():
    parser = argparse.ArgumentParser(description="Генерация и сверка финансовых событий.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--verify", action="store_true", help="Сверить результат обработки в PostgreSQL."
    )
    args = parser.parse_args()

    async def run():
        try:
            if args.verify:
                async with session_factory() as session:
                    return await verify_events(session, args.seed)
            key = os.environ.get("INTEGRATION_KEY")
            if not key:
                raise ValueError("Укажите ключ источника в переменной INTEGRATION_KEY.")
            async with httpx.AsyncClient(base_url=args.api_url, timeout=60) as client:
                return await send_events(session_factory, client, key, args.seed, args.batch_size)
        finally:
            await engine.dispose()

    try:
        result = asyncio.run(run())
    except (ValueError, httpx.HTTPError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"scenario": "synthetic", "seed": args.seed, **result}, default=str))


if __name__ == "__main__":
    main()
