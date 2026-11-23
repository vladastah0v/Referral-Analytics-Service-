"""Exercise the running API and the scheduler against a disposable Compose project."""

import os
import sys
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from uuid import uuid4

import httpx


def main() -> None:
    integration_key = sys.stdin.read().strip()
    if not integration_key:
        raise RuntimeError("A test integration key is required on stdin")
    with httpx.Client(
        base_url=os.getenv("SMOKE_API_URL", "http://127.0.0.1:8000"), timeout=5
    ) as client:
        deadline = monotonic() + 60
        while True:
            try:
                client.get("/health/ready").raise_for_status()
                break
            except httpx.HTTPError:
                if monotonic() >= deadline:
                    raise
                sleep(1)
        assert client.get("/docs").status_code == 200

        account = {
            "email": "smoke@example.com",
            "full_name": "CI Smoke",
            "password": "Synthetic-ci-password",
        }
        buyer = client.post("/api/v1/auth/register", json=account).raise_for_status().json()
        login = client.post(
            "/api/v1/auth/login", json={name: account[name] for name in ("email", "password")}
        ).raise_for_status()
        user_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        now = datetime.now(UTC)
        order = {"external_order_id": str(uuid4()), "currency": "KZT"}
        messages = [
            {
                "external_event_id": str(uuid4()),
                "event_type": "order_created",
                "occurred_at": now.isoformat(),
                "payload": order | {"user_id": buyer["id"], "total_amount": "100.00"},
            },
            {
                "external_event_id": str(uuid4()),
                "event_type": "order_paid",
                "occurred_at": (now + timedelta(seconds=1)).isoformat(),
                "payload": order | {"operation_id": str(uuid4()), "amount": "100.00"},
            },
        ]
        receipt = client.post(
            "/api/v1/events/batch",
            json={"events": messages},
            headers={"X-Integration-Key": integration_key},
        )
        assert receipt.status_code == 207
        assert [item["status_code"] for item in receipt.json()["results"]] == [202, 202]
        deadline = monotonic() + 60
        while monotonic() < deadline:
            data = client.get("/api/v1/orders", headers=user_headers).raise_for_status().json()
            if len(data["items"]) == 1 and data["items"][0]["status"] == "paid":
                print("API, PostgreSQL, Redis, Celery worker and beat verified")
                return
            sleep(1)
        raise RuntimeError("Scheduled outbox processing did not record the payment in time")


if __name__ == "__main__":
    main()
