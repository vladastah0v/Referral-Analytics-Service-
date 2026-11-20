"""Run Locust against a dedicated seeded benchmark environment."""

import argparse
import asyncio
import json
import os
import platform
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import engine, session_factory
from app.core.security import create_token, hash_refresh_token
from app.modules.auth.models import AuthSession
from app.modules.referrals.models import ReferralAttribution, ReferralClick, ReferralCode
from app.modules.tracking.security import create_credential
from app.modules.users.models import User, UserRole
from app.workers.celery_app import celery_app
from scripts.seed import Scenario

LOCUSTFILE = Path(__file__).resolve().parents[1] / "tests/performance/locustfile.py"


def grant(session, user_id):
    session_id = uuid4()
    token, _ = create_token(user_id, session_id, "access")
    refresh, expires = create_token(user_id, session_id, "refresh")
    session.add(
        AuthSession(
            id=session_id,
            user_id=user_id,
            expires_at=expires,
            refresh_token_hash=hash_refresh_token(refresh),
        )
    )
    return token, session_id


async def prepare(seed, source):
    async with session_factory() as session:
        row = (
            await session.execute(
                select(ReferralAttribution, ReferralClick, ReferralCode)
                .join(ReferralClick, ReferralClick.id == ReferralAttribution.click_id)
                .join(ReferralCode, ReferralCode.id == ReferralClick.referral_code_id)
                .join(User, User.id == ReferralCode.owner_id)
                .where(User.email == Scenario.email_for_seed(seed, 0))
                .order_by(ReferralAttribution.id)
                .limit(1)
            )
        ).first()
        if row is None:
            raise ValueError("Seed должен содержать атрибуцию первого реферера.")
        attribution, click, code = row
        admin = User(
            email=f"{source}@example.com",
            full_name="Synthetic Benchmark",
            role=UserRole.ADMIN,
            password_hash="!",
        )
        session.add(admin)
        await session.flush()
        owner_token, owner_session = grant(session, code.owner_id)
        admin_token, admin_session = grant(session, admin.id)
        credential, key = await create_credential(session, source)
        return dict(
            code=code.code,
            clicked_at=click.clicked_at.isoformat(),
            click_id=str(click.id),
            order_at=(attribution.attributed_at + timedelta(seconds=1)).isoformat(),
            buyer_id=str(attribution.referred_user_id),
            owner_token=owner_token,
            admin_token=admin_token,
            key=key,
        ), (credential, admin, owner_session, admin_session)


async def counters(cache):
    async with engine.connect() as conn:
        pg = (
            await conn.execute(
                text("""
            SELECT COALESCE(sum(calls), 0)::bigint, COALESCE(sum(total_exec_time), 0)
            FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database
                                                   WHERE datname = current_database())
            AND query NOT LIKE '%pg_stat_statements%'
        """)
            )
        ).one()
    info = await cache.info("stats")
    return (*pg, info["keyspace_hits"], info["keyspace_misses"])


def interval(before, after):
    calls, elapsed, hits, misses = (b - a for a, b in zip(before, after, strict=True))
    if min(calls, elapsed, hits, misses) < 0:
        raise ValueError("Счётчики стенда сбросились во время измерения.")
    return dict(
        pg_calls=calls,
        pg_total_exec_ms=round(elapsed, 3),
        pg_mean_exec_ms=round(elapsed / calls, 3) if calls else None,
        redis_hits=hits,
        redis_misses=misses,
        redis_hit_ratio=round(hits / (hits + misses), 6) if hits + misses else None,
    )


async def run_phase(args, output, cache, fixture, name):
    before = await counters(cache)
    command = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        str(LOCUSTFILE),
        "--headless",
        "--host",
        args.host,
        "--users",
        str(args.users),
        "--spawn-rate",
        str(args.spawn_rate),
        "--run-time",
        f"{args.seconds}s",
        "--stop-timeout",
        "10",
        "--csv",
        str(output / name),
    ]
    if name == "read":
        command += ["--tags", "read"]
    proc = await asyncio.create_subprocess_exec(
        *command,
        env=os.environ | {"REFERRAL_BENCH_FIXTURE": json.dumps(fixture)},
    )
    try:
        exit_code = await proc.wait()
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
    return interval(before, await counters(cache)) | {"locust_exit_code": exit_code}


async def schedule_parallel_drains():
    while True:
        jobs = [
            await asyncio.to_thread(celery_app.send_task, "tracking.drain_outbox") for _ in range(2)
        ]
        while not await asyncio.to_thread(lambda jobs=jobs: all(job.ready() for job in jobs)):
            await asyncio.sleep(1)
        await asyncio.sleep(2)


async def await_rewards(source, timeout):
    deadline = time.monotonic() + timeout
    while True:
        async with engine.connect() as conn:
            result = (
                (
                    await conn.execute(
                        text("""
                SELECT count(*) FILTER (WHERE processing_status = 'pending') AS pending,
                       count(*) FILTER (WHERE processing_status = 'rejected') AS rejected,
                       count(*) FILTER (WHERE event_type = 'order_created') AS created_events
                FROM events WHERE source_system = :source
            """),
                        {"source": source},
                    )
                )
                .mappings()
                .one()
            )
            if not result["pending"] or time.monotonic() >= deadline:
                reward = (
                    (
                        await conn.execute(
                            text("""
                    SELECT count(*) AS orders, count(f.id) AS payments, count(r.id) AS rewards,
                           count(*) FILTER (WHERE r.reward_amount = 5.00
                                            AND f.amount = 100.00) AS correct_amounts
                    FROM orders o
                    LEFT JOIN order_financial_events f
                        ON f.order_id = o.id AND f.event_type = 'payment'
                    LEFT JOIN referral_rewards r ON r.order_id = o.id
                    WHERE o.source_system = :source
                """),
                            {"source": source},
                        )
                    )
                    .mappings()
                    .one()
                )
                return dict(result) | dict(reward)
        await asyncio.sleep(2)


async def benchmark(args):
    settings = get_settings()
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    source = f"benchmark-{secrets.token_hex(8)}"
    try:
        async with Redis.from_url(settings.redis_url) as cache:
            await counters(cache)
            fixture, credentials = await prepare(args.seed, source)
            try:
                async with engine.connect() as conn:
                    rows = {}
                    for table in (
                        "users",
                        "referral_clicks",
                        "referral_attributions",
                        "leads",
                        "orders",
                    ):
                        rows[table] = await conn.scalar(text(f"SELECT count(*) FROM {table}"))
                    rows["users"] -= 1  # The runner just added its temporary administrator.
                    postgres = await conn.scalar(text("SHOW server_version"))
                report = dict(
                    started_at=datetime.now(UTC).isoformat(),
                    source=source,
                    seed=args.seed,
                    users=args.users,
                    spawn_rate=args.spawn_rate,
                    seconds_per_phase=args.seconds,
                    initial_rows=rows,
                    platform=platform.platform(),
                    python=platform.python_version(),
                    locust=version("locust"),
                    postgres=postgres,
                    redis=(await cache.info("server"))["redis_version"],
                )
                report["read"] = await run_phase(args, output, cache, fixture, "read")
                async with asyncio.TaskGroup() as tasks:
                    drains = tasks.create_task(schedule_parallel_drains())
                    report["mixed"] = await run_phase(args, output, cache, fixture, "mixed")
                    drain_started = time.monotonic()
                    report["outbox"] = await await_rewards(source, args.drain_timeout)
                    report["drain_seconds"] = round(time.monotonic() - drain_started, 3)
                    drains.cancel()
                (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
                result = report["outbox"]
                return int(
                    any(report[name]["locust_exit_code"] for name in ("read", "mixed"))
                    or result["pending"]
                    or result["rejected"]
                    or not result["created_events"]
                    or any(
                        result[key] != result["created_events"]
                        for key in ("orders", "payments", "rewards", "correct_amounts")
                    )
                )
            finally:
                credential, admin, owner_session, admin_session = credentials
                async with session_factory() as session:
                    for session_id in (owner_session, admin_session):
                        active = await session.get(AuthSession, session_id)
                        if active is not None:
                            await session.delete(active)
                    await session.delete(credential)
                    await session.delete(admin)
                    await session.commit()
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(
        description="Нагрузочный тест на отдельном синтетическом стенде."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--users", type=int, default=8)
    parser.add_argument("--spawn-rate", type=float, default=4)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--drain-timeout", type=int, default=180)
    args = parser.parse_args()
    if min(args.users, args.spawn_rate, args.seconds, args.drain_timeout) <= 0:
        parser.error(
            "Число пользователей, скорость запуска и интервалы должны быть положительными."
        )
    if args.seconds * 2 + 30 >= get_settings().jwt_access_ttl_minutes * 60:
        parser.error("Обе фазы должны укладываться в TTL access-токена.")
    return asyncio.run(benchmark(args))


if __name__ == "__main__":
    raise SystemExit(main())
