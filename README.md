# Referral Analytics Service

## Project Overview

Referral Analytics Service connects referral links to registrations, CRM leads, and customer orders. It tracks attributable revenue and maintains a referral reward history as payments, refunds, and adjustments are processed. Customers can see their own referrals and rewards. Managers and administrators can monitor the funnel and revenue within their access scope.

## Features

- JWT authentication with rotating refresh tokens and role-based access.
- Referral codes with optional expiry and attribution limits, idempotent click tracking, and a 30-day last-click attribution window.
- CRM lead assignment, controlled status transitions, and status history.
- Batch event ingestion, a transactional outbox, and an idempotent financial ledger.
- Fixed or percentage rewards with rule snapshots, refund recalculation, and administrator approval.
- Analytics by currency, time period, referral code, campaign, and source, with Redis caching and daily PostgreSQL snapshots.
- Prometheus metrics, a provisioned Grafana dashboard, and structured JSON logs.

## Tech Stack

| Layer | Technology |
| --- | --- |
| API | Python 3.12, FastAPI, Pydantic, Uvicorn |
| Data | PostgreSQL 16, SQLAlchemy async, asyncpg, Alembic |
| Cache and background processing | Redis 7, Celery worker, Celery beat |
| Operations | Docker Compose, Prometheus, Grafana |
| Verification | Ruff, pytest, pytest-cov, Locust, GitHub Actions |

## Architecture

The API writes domain data to PostgreSQL and accepts integration notifications through the tracking module. PostgreSQL is the source of truth for orders, financial operations, and rewards. Celery processes the outbox and refreshes daily snapshots. Redis handles task delivery and report caching. Prometheus scrapes the API and worker independently. Grafana reads from Prometheus. [Architecture and data diagrams](docs/architecture.md)

## Database Schema

| Tables | Responsibility |
| --- | --- |
| `users`, `auth_sessions` | Accounts, roles, and refresh sessions |
| `referral_programs`, `referral_codes`, `referral_clicks`, `referral_attributions` | Reward rules, referral links, clicks, and one attribution per referred user |
| `leads`, `lead_status_history` | CRM assignment, status, and transition history |
| `integration_credentials`, `events`, `event_outbox` | Integration identity, incoming events, and processing state |
| `orders`, `order_financial_events` | Order state, frozen attribution, and financial operations |
| `referral_rewards`, `referral_reward_history` | One reward per eligible order, the rules applied at payment, and subsequent changes |
| `analytics_cache_revisions`, `analytics_daily_snapshots`, `analytics_snapshot_requests`, `analytics_snapshot_failures` | Cache versioning and daily snapshot storage, scheduling, and recovery |

See the [ER diagram and transaction boundaries](docs/architecture.md#основные-связи-данных).

## Quick Start

Run from the repository root with Docker Compose. On a fresh checkout, copy the local configuration and apply migrations before starting the API and workers.

```bash
cp .env.example .env
docker compose up -d postgres redis
docker compose run --rm --build api alembic upgrade head
docker compose up -d --build
curl --fail http://127.0.0.1:8000/health/ready
```

Open [Swagger UI](http://127.0.0.1:8000/docs) or fetch the [OpenAPI schema](http://127.0.0.1:8000/openapi.json). To initialize an administrator and issue an integration key, run these commands in an interactive terminal.

```bash
docker compose exec api python -m app.cli create-admin --email admin@example.com --full-name "Administrator"
docker compose exec api python -m app.cli create-integration --source shop
```

The administrator command prompts for a password. Store the issued integration key securely. For deployment, supply your own database credentials and JWT secret and serve external traffic over HTTPS. [Authentication](docs/authentication.md), [Integration credentials](docs/tracking.md).

Enable the optional monitoring profile with `docker compose --profile monitoring up -d`. Prometheus listens on local port 9090 and Grafana on local port 3000. [Monitoring setup and JSON logs](docs/monitoring.md)

## API Documentation

Business routes use `/api/v1`. Protected user routes use `Authorization: Bearer <access_token>`. Event ingestion uses `X-Integration-Key`.

| Area | Main routes |
| --- | --- |
| Authentication | `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /users/me` |
| Referrals | `POST /referrals/track`, `GET /referrals/statistics`, `/referrals/codes`, `/admin/referral-programs` |
| CRM | `GET /leads`, `POST /leads`, `PATCH /leads/{lead_id}/status`, `PATCH /leads/{lead_id}/assign`, `GET /leads/{lead_id}/history` |
| Integration | `POST /events`, `POST /events/batch`, `GET /events/{event_id}` |
| Orders and rewards | `GET /orders`, `GET /rewards/me`, `GET /admin/rewards`, `PATCH /admin/rewards/{reward_id}/approve` |
| Analytics | `GET /analytics/overview`, `/analytics/referrals`, `/analytics/funnel`, `/analytics/revenue`, `/analytics/top-referrers`, `/analytics/conversion` |

A valid new event returns `202` and an event ID. An identical retry returns `200`. Reusing the same external event ID within a source with different data returns `409`. Batch ingestion accepts up to 100 events and returns `207` with a status for each item. [Event payloads and processing states](docs/tracking.md)

## Referral Attribution Logic

1. The client sends a stable random UUIDv4 as `visitor_id` and a per-click UUIDv4 as `request_id` to the tracking endpoint.
2. Registration with the same visitor ID searches the preceding 30 days. It selects the latest eligible click, using the click UUID to break timestamp ties.
3. Eligibility requires an active, unexpired code, an available attribution slot when a limit exists, and a different account from the referrer. The service checks and records attribution inside the registration transaction.
4. Order creation freezes the buyer’s attribution as of the creation event. The first confirmed payment selects the matching active reward program by currency and payment time. Refunds and adjustments recalculate the reward under those saved rules.

[Attribution sequence](docs/architecture.md#атрибуция-регистрации), [Referral rules](docs/referrals.md), [CRM](docs/crm.md), [Order lifecycle](docs/orders.md), [Reward calculation](docs/rewards.md).

## Analytics Metrics

| Metric | Definition |
| --- | --- |
| Referral reach | Recorded clicks and distinct visitors across the selected codes |
| Referral conversion | Attributed registrations divided by distinct visitors, expressed as a percentage. Clicks and registrations are selected on their respective timestamps. |
| CRM conversion | Converted leads divided by leads created in the selected period |
| Net revenue | Confirmed payments plus credit adjustments, minus refunds and debit adjustments |
| Attributed revenue | Net revenue assigned to orders with a frozen referral attribution |
| Reward totals | Changes in reward amounts recorded in reward history |

Time filters use UTC with an inclusive start and exclusive end. Monetary totals are kept separate by currency. Report cache keys include the user, role, filters, and the PostgreSQL data revision. [Analytics filters, aggregation, and caching](docs/analytics.md)

## Background Processing

The API inserts an accepted event and its outbox row in one transaction. Workers lock available rows with `SKIP LOCKED`. A financial operation, any resulting reward revision, and the event status commit together. Retries can safely repeat a delivery. Beat schedules outbox processing, changed-day refreshes, daily snapshots, and expired session cleanup. [Event processing diagram, schedules, and recovery commands](docs/workers.md)

## Testing

GitHub Actions runs Ruff, applies and checks migrations on PostgreSQL, runs pytest with Redis, enforces 70% application coverage, and builds the Docker image. [Workflow](.github/workflows/ci.yml)

Run unit tests locally with `uv run --locked pytest tests/unit`. For the full suite, start dedicated disposable PostgreSQL and Redis instances and set their connection URLs. Integration tests apply migrations and clear business tables between cases. The PostgreSQL database name must begin with `test_`.

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://ci:ci-local@127.0.0.1:5432/test_referral_ci
export TEST_REDIS_URL=redis://127.0.0.1:6379/14
uv run --locked pytest --cov=app --cov-report=term-missing --cov-fail-under=70
```

## Performance Benchmarks

The recorded Locust run used a synthetic dataset with 10,000 users and 30,000 clicks. Each phase ramped up to eight virtual users and ran for 30 seconds on a local Mac with PostgreSQL and Redis in Docker.

| Phase | Requests | Requests/s | p95 | p99 | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| Read | 413 | 14.24 | 53 ms | 180 ms | 0 |
| Mixed | 367 | 12.64 | 290 ms | 440 ms | 0 |

These measurements describe the recorded workload and hardware. [Reproduction guide, PostgreSQL and Redis measurements, and raw CSV](docs/benchmark.md), [Synthetic data commands](docs/seed.md).

## Project Structure

```text
app/core/               Settings, database, authentication, logs, and metrics
app/modules/            Users, auth, referrals, CRM, orders, rewards, tracking, analytics
app/workers/            Celery tasks, scheduler configuration, and worker metrics
migrations/             Alembic migrations
scripts/                Synthetic dataset, event generation, and benchmarking
tests/                  Unit, PostgreSQL integration, and Locust workloads
infra/                  Prometheus and Grafana configuration
docs/                   Architecture, business rules, operations, and benchmark results
.github/workflows/      CI workflow
```

## Roadmap

Potential extensions include reconciliation tooling for operators, longer benchmark runs with additional deployment profiles, and configurable alert thresholds for outbox delays and failed tasks.
