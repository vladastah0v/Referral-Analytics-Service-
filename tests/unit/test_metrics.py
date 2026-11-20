from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.core.metrics import HTTPMetricsMiddleware
from app.main import app


def sample(name, **labels):
    return REGISTRY.get_sample_value(f"referral_http_{name}", labels) or 0


def test_metrics_expose_route_template_and_http_errors():
    event_id = str(uuid4())
    labels = {"method": "GET", "route": "/api/v1/events/{event_id}", "status": "401"}
    before = sample("requests_total", **labels)
    errors_before = sample("errors_total", **labels)
    duration_labels = {"method": labels["method"], "route": labels["route"]}
    duration_before = sample("request_duration_seconds_count", **duration_labels)
    with TestClient(app) as client:
        assert (
            client.get(f"/api/v1/events/{event_id}?email=monitoring@example.com").status_code == 401
        )
        assert client.request("CUSTOM", f"/missing/{event_id}").status_code == 404
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/plain")
    assert event_id not in response.text
    assert "monitoring@example.com" not in response.text
    assert 'route="/metrics"' not in response.text
    assert 'method="OTHER",route="unmatched",status="404"' in response.text
    assert sample("requests_total", **labels) == before + 1
    assert sample("errors_total", **labels) == errors_before + 1
    assert sample("request_duration_seconds_count", **duration_labels) == duration_before + 1


def test_unhandled_exception_is_counted_as_500():
    api = FastAPI()
    api.add_middleware(HTTPMetricsMiddleware)

    @api.get("/metrics-test-error")
    async def fail():
        raise RuntimeError("test error")

    labels = {"method": "GET", "route": "/metrics-test-error", "status": "500"}
    before = sample("errors_total", **labels)
    with TestClient(api, raise_server_exceptions=False) as client:
        assert client.get("/metrics-test-error").status_code == 500
    assert sample("errors_total", **labels) == before + 1
