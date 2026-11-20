"""Generate a reproducible referral dataset. Run with python -m scripts.seed."""

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import batched
from random import Random
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.database import engine
from app.modules.leads.models import Lead, LeadStatusHistory
from app.modules.orders.models import Order
from app.modules.referrals.models import (
    ReferralAttribution,
    ReferralClick,
    ReferralCode,
    ReferralProgram,
)
from app.modules.users.models import User

CAMPAIGNS = ("partners", "newsletter", "community", "ambassadors")
SOURCES = ("partner", "email", "social", "direct")
SECOND = timedelta(seconds=1)


@dataclass(frozen=True)
class Scenario:
    seed: int
    start: date
    days: int
    users: int
    clicks: int
    referrals: int
    leads: int
    orders: int

    @property
    def referrers(self) -> int:
        return min(40, max(1, self.users // 100))

    @property
    def managers(self) -> int:
        return min(6, self.users // 100)

    def validate(self) -> None:
        if not -(2**63) <= self.seed < 2**63:
            raise ValueError("Seed должен быть целым 64-битным числом.")
        if self.days < 1 or self.start < date(2000, 1, 1):
            raise ValueError("Интервал должен начинаться с 2000 года и включать хотя бы сутки.")
        if self.days > (datetime.now(UTC).date() - self.start).days:
            raise ValueError("Выберите интервал из завершённых суток UTC.")
        if self.users < 2 or min(self.clicks, self.referrals, self.leads, self.orders) < 0:
            raise ValueError(
                "Нужно минимум два пользователя. Количества не могут быть отрицательны."
            )
        if self.referrals > min(self.clicks, self.users - self.referrers - self.managers):
            raise ValueError("Для каждой атрибуции нужны отдельный клиент и переход.")

    @property
    def namespace(self) -> UUID:
        # Version the generator so a changed algorithm cannot silently reuse an old seed.
        parameters = json.dumps(asdict(self), sort_keys=True, default=str)
        return uuid5(NAMESPACE_URL, f"referral-analytics/seed/v1/{parameters}")

    def email(self, index: int) -> str:
        return self.email_for_seed(self.seed, index)

    @staticmethod
    def email_for_seed(seed: int, index: int) -> str:
        return f"referral-seed-{seed}-{index}@example.com"


def entity_id(namespace: UUID, kind: str, index: int | str) -> UUID:
    # The public API validates visitor and request identifiers as UUID4.
    return UUID(bytes=uuid5(namespace, f"{kind}/{index}").bytes, version=4)


def build_rows(scenario: Scenario):
    rng = Random(scenario.seed)
    namespace = scenario.namespace
    start = datetime.combine(scenario.start, time.min, UTC)
    duration = scenario.days * 86400
    end = start + duration * SECOND
    buyers_from = scenario.referrers + scenario.managers
    users = []
    codes = []
    clicks = []
    attrs = []
    leads = []
    history = []
    orders = []
    for index in range(scenario.users):
        registered = start
        if index >= buyers_from:
            registered += SECOND * rng.randint(3, duration - 12)
        users.append(
            dict(
                id=entity_id(namespace, "user", index),
                email=scenario.email(index),
                full_name=f"Synthetic User {index + 1}",
                password_hash="!",
                role="manager" if scenario.referrers <= index < buyers_from else "customer",
                created_at=registered,
                updated_at=registered,
            )
        )
    for index in range(scenario.referrers):
        code_id = entity_id(namespace, "code", index)
        codes.append(
            dict(
                id=code_id,
                owner_id=users[index]["id"],
                code=f"syn-{code_id.hex}",
                created_at=start + SECOND,
            )
        )
    for index in range(scenario.clicks):
        code = rng.choice(codes)
        click_id = entity_id(namespace, "click", index)
        if index < scenario.referrals:
            buyer = users[buyers_from + index]
            registered = buyer["created_at"]
            window = min(30 * 86400, (registered - start) // SECOND - 2)
            clicked = registered - rng.randint(1, window) * SECOND
            visitor_id = entity_id(namespace, "referred-visitor", index)
            attrs.append(
                dict(
                    id=entity_id(namespace, "attribution", index),
                    referral_code_id=code["id"],
                    referrer_id=code["owner_id"],
                    referred_user_id=buyer["id"],
                    click_id=click_id,
                    attributed_at=registered,
                    attribution_model="last_referral_click",
                )
            )
        else:
            clicked = start + rng.randrange(2, duration) * SECOND
            visitor = rng.randrange(max(1, (scenario.clicks - scenario.referrals) // 2))
            visitor_id = entity_id(namespace, "anonymous-visitor", visitor)
        channel = rng.randrange(len(CAMPAIGNS))
        clicks.append(
            dict(
                id=click_id,
                referral_code_id=code["id"],
                visitor_id=visitor_id,
                clicked_at=clicked,
                source=SOURCES[channel],
                campaign=CAMPAIGNS[channel],
                request_id=entity_id(namespace, "click-request", index),
            )
        )

    attribution = {row["referred_user_id"]: row["id"] for row in attrs}

    def customer_action():
        user = users[rng.randrange(buyers_from, len(users))]
        available = (end - user["created_at"]) // SECOND - 6
        at = user["created_at"] + rng.randint(1, min(available, 7 * 86400)) * SECOND
        return user["id"], at

    for index in range(scenario.leads):
        uid, created = customer_action()
        lead_id = entity_id(namespace, "lead", index)
        manager = None
        stages = ["new"]
        if scenario.managers:
            manager = users[rng.randrange(scenario.referrers, buyers_from)]["id"]
            stages = rng.choice(
                (
                    ["new"],
                    ["new", "lost"],
                    ["new", "contacted"],
                    ["new", "contacted", "qualified"],
                    ["new", "contacted", "qualified", "converted"],
                )
            )
        dates = {stage: created + i * SECOND for i, stage in enumerate(stages)}
        previous = None
        for stage in stages:
            history.append(
                dict(
                    id=entity_id(lead_id, "history", stage),
                    lead_id=lead_id,
                    previous_status=previous,
                    new_status=stage,
                    changed_by=uid if previous is None else manager,
                    changed_at=dates[stage],
                )
            )
            previous = stage
        leads.append(
            dict(
                id=lead_id,
                user_id=uid,
                referral_attribution_id=attribution.get(uid),
                assigned_manager_id=manager,
                source=rng.choice(SOURCES),
                status=stages[-1],
                created_at=created,
                updated_at=dates[stages[-1]],
                qualified_at=dates.get("qualified"),
                converted_at=dates.get("converted"),
            )
        )
    for index in range(scenario.orders):
        uid, created = customer_action()
        orders.append(
            dict(
                id=entity_id(namespace, "order", index),
                external_order_id=f"order-{index + 1}",
                source_system=f"synthetic-{scenario.seed}",
                user_id=uid,
                status="created",
                total_amount=Decimal(rng.randint(100_000, 10_000_000)).scaleb(-2),
                currency="KZT",
                referral_attribution_id=attribution.get(uid),
                attribution_frozen_at=created,
                external_created_at=created,
                created_at=created,
                updated_at=created,
            )
        )
    return [
        (User, users),
        (
            ReferralProgram,
            [
                dict(
                    id=entity_id(namespace, "program", 0),
                    name=f"Synthetic {scenario.seed}",
                    reward_type="percentage",
                    reward_value=Decimal("5.00"),
                    currency="KZT",
                    starts_at=start,
                    ends_at=end,
                )
            ],
        ),
        (ReferralCode, codes),
        (ReferralClick, clicks),
        (ReferralAttribution, attrs),
        (Lead, leads),
        (LeadStatusHistory, history),
        (Order, orders),
    ]


async def seed_database(db: AsyncEngine, scenario: Scenario) -> bool:
    scenario.validate()
    async with db.begin() as connection:
        first_email = scenario.email(0)
        await connection.execute(
            select(func.pg_advisory_xact_lock(0x53454544, func.hashtext(first_email)))
        )
        existing = await connection.scalar(select(User.id).where(User.email == first_email))
        if existing is not None:
            if existing != entity_id(scenario.namespace, "user", 0):
                raise ValueError("Этот seed уже занят набором с другими параметрами.")
            return False
        for model, rows in build_rows(scenario):
            for batch in batched(rows, 500):
                await connection.execute(insert(model.__table__).values(list(batch)))
    return True


def main():
    parser = argparse.ArgumentParser(description="Загрузка синтетического набора в DATABASE_URL.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2025, 1, 1))
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--users", type=int, default=10_000)
    parser.add_argument("--clicks", type=int)
    parser.add_argument("--referrals", type=int)
    parser.add_argument("--leads", type=int)
    parser.add_argument("--orders", type=int)
    args = parser.parse_args()
    clicks = args.users * 3 if args.clicks is None else args.clicks
    scenario = Scenario(
        seed=args.seed,
        start=args.start_date,
        days=args.days,
        users=args.users,
        clicks=clicks,
        referrals=min(args.users // 5, clicks) if args.referrals is None else args.referrals,
        leads=args.users // 2 if args.leads is None else args.leads,
        orders=args.users // 2 if args.orders is None else args.orders,
    )

    async def run():
        try:
            return await seed_database(engine, scenario)
        finally:
            await engine.dispose()

    try:
        created = asyncio.run(run())
    except ValueError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            dict(
                scenario="synthetic",
                seed=scenario.seed,
                status="created" if created else "already_exists",
                start=scenario.start.isoformat(),
                days=scenario.days,
                users=scenario.users,
                clicks=scenario.clicks,
                referrals=scenario.referrals,
                leads=scenario.leads,
                orders=scenario.orders,
            )
        )
    )


if __name__ == "__main__":
    main()
