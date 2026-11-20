import json
import logging
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.logging import JSONFormatter
from app.main import app


def test_http_access_record_contains_only_route_template_and_metadata(caplog):
    event_id = str(uuid4())
    with caplog.at_level(logging.INFO, logger="app.http"), TestClient(app) as client:
        response = client.get(
            f"/api/v1/events/{event_id}?email=monitoring@example.com",
            headers={"X-Integration-Key": "example-secret"},
        )
        assert response.status_code == 401
    record = next(record for record in caplog.records if record.name == "app.http")
    encoded = JSONFormatter("api").format(record)
    data = json.loads(encoded)
    assert data["route"] == "/api/v1/events/{event_id}"
    assert data["method"] == "GET"
    assert data["status"] == 401
    assert data["duration_ms"] >= 0
    for value in (event_id, "monitoring@example.com", "example-secret"):
        assert value not in encoded


def test_formatter_keeps_error_type_and_allowlisted_context():
    try:
        raise ValueError("private exception text")
    except ValueError as exc:
        record = logging.LogRecord(
            "app.workers.runtime",
            logging.ERROR,
            __file__,
            1,
            "Background operation failed",
            (),
            (type(exc), exc, exc.__traceback__),
        )
    record.task_id = "task-id"
    record.private = "private field"
    encoded = JSONFormatter("celery").format(record)
    data = json.loads(encoded)
    assert data["error_type"] == "ValueError"
    assert data["task_id"] == "task-id"
    assert data["timestamp"].endswith("+00:00")
    assert "private" not in encoded
