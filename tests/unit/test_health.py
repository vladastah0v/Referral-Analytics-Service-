from fastapi.testclient import TestClient

from app.main import app, engine


def test_liveness() -> None:
    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_returns_503_when_dependencies_unavailable(monkeypatch) -> None:
    def unavailable(self):
        raise OSError("connection unavailable")

    monkeypatch.setattr(type(engine), "connect", unavailable)
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Dependencies unavailable"}
