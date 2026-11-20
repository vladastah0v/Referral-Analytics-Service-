"""HTTP workloads used by scripts.benchmark."""

import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

from locust import FastHttpUser, between, tag, task

FIXTURE = json.loads(os.environ.get("REFERRAL_BENCH_FIXTURE", "{}"))


class ReferralUser(FastHttpUser):
    wait_time = between(0.35, 0.7)
    network_timeout = 10
    insecure = False

    @tag("read")
    @task(3)
    def statistics(self):
        self.client.get(
            "/api/v1/referrals/statistics",
            headers={"Authorization": f"Bearer {FIXTURE['owner_token']}"},
        )

    @tag("read")
    @task(3)
    def overview(self):
        self.client.get(
            "/api/v1/analytics/overview",
            headers={"Authorization": f"Bearer {FIXTURE['admin_token']}"},
        )

    @task(3)
    def track(self):
        self.client.post(
            "/api/v1/referrals/track",
            json={
                "code": FIXTURE["code"],
                "visitor_id": str(uuid4()),
                "request_id": str(uuid4()),
                "campaign": "synthetic-benchmark",
                "source": "benchmark",
            },
        )

    def send_batch(self, name, messages):
        with self.rest(
            "POST",
            "/api/v1/events/batch",
            name=name,
            json={"events": messages},
            headers={"X-Integration-Key": FIXTURE["key"]},
        ) as response:
            if response.status_code == 207:
                results = (response.js or {}).get("results", [])
                if len(results) != len(messages) or any(
                    item["status_code"] != 202 for item in results
                ):
                    response.failure("Batch contains incomplete or rejected items")
            else:
                response.failure(f"Expected 207, received {response.status_code}")

    @task(1)
    def batch(self):
        self.send_batch(
            "batch / referral confirmations",
            [
                {
                    "external_event_id": str(uuid4()),
                    "event_type": "referral_clicked",
                    "occurred_at": FIXTURE["clicked_at"],
                    "payload": {"click_id": FIXTURE["click_id"]},
                }
                for _ in range(10)
            ],
        )

    @task(2)
    def rewards(self):
        order = str(uuid4())
        created = datetime.fromisoformat(FIXTURE["order_at"])
        payment = {
            "event_type": "order_paid",
            "occurred_at": (created + timedelta(seconds=1)).isoformat(),
            "payload": {
                "external_order_id": order,
                "operation_id": order,
                "amount": "100.00",
                "currency": "KZT",
            },
        }
        self.send_batch(
            "batch / reward accrual",
            [
                {
                    "external_event_id": f"{order}-create",
                    "event_type": "order_created",
                    "occurred_at": created.isoformat(),
                    "payload": {
                        "external_order_id": order,
                        "user_id": FIXTURE["buyer_id"],
                        "total_amount": "100.00",
                        "currency": "KZT",
                    },
                },
                payment | {"external_event_id": f"{order}-pay"},
                payment | {"external_event_id": f"{order}-duplicate"},
            ],
        )
