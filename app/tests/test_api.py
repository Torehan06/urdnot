from datetime import datetime
import logging
import socket

from fastapi.testclient import TestClient
import psycopg
import pytest
import redis

from app.main import create_app


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    for key in ("DATABASE_URL", "REDIS_URL", "CLAN_NAME", "CLAN_SECRET", "APP_VERSION"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def client():
    with TestClient(create_app()) as session:
        yield session


def test_healthz(client):
    assert client.get("/healthz").status_code == 200


def test_ready_without_dependencies(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_hollows_create_and_list(client):
    assert client.get("/api/hollows").json() == []
    payload = {"name": "Shiagur", "text": "Remembered in the Hollows."}
    response = client.post("/api/hollows", json=payload,
                           headers={"X-Clan-Secret": "shiagur"})
    assert response.status_code == 201
    record = response.json()
    assert set(record) == {"id", "name", "text", "created_at"}
    assert record["name"] == payload["name"]
    assert record["text"] == payload["text"]
    assert record["id"]
    assert datetime.fromisoformat(record["created_at"]).tzinfo is not None
    assert client.get("/api/hollows").json() == [record]


@pytest.mark.parametrize("headers", [{}, {"X-Clan-Secret": "wrong"}])
def test_hollows_requires_secret(client, headers):
    response = client.post("/api/hollows", json={"name": "A", "text": "B"},
                           headers=headers)
    assert response.status_code == 403
    assert client.get("/api/hollows").json() == []


def test_headbutts_increment(client):
    assert client.get("/api/headbutts").json() == {"count": 1}
    assert client.get("/api/headbutts").json() == {"count": 2}


def test_charge_consumes_cpu(client):
    response = client.get("/api/charge?ms=40")
    assert response.status_code == 200
    result = response.json()
    assert result["requested_ms"] == 40
    assert 40 <= result["cpu_ms"] < 500
    assert 35 <= result["duration_ms"] < 2000


@pytest.mark.parametrize("ms", [0, -1, 10001, "invalid"])
def test_charge_bounds(client, ms):
    assert client.get(f"/api/charge?ms={ms}").status_code == 422


def test_request_time_config(client, monkeypatch):
    monkeypatch.setenv("CLAN_NAME", "<New Clan>")
    monkeypatch.setenv("CLAN_SECRET", "new-secret")
    page = client.get("/").text
    assert "&lt;New Clan&gt;" in page
    assert socket.gethostname() in page
    assert "in-memory" in page
    payload = {"name": "A", "text": "B"}
    assert client.post("/api/hollows", json=payload,
                       headers={"X-Clan-Secret": "shiagur"}).status_code == 403
    assert client.post("/api/hollows", json=payload,
                       headers={"X-Clan-Secret": "new-secret"}).status_code == 201


def test_metrics_and_access_log(client, caplog):
    with caplog.at_level(logging.INFO, logger="urdnot-api.access"):
        client.get("/healthz")
        client.post("/api/hollows", json={"name": "A", "text": "B"})
        client.get("/unknown-path")
    records = [r for r in caplog.records if r.name == "urdnot-api.access"]
    assert len(records) == 3
    assert records[0].fields["method"] == "GET"
    assert records[0].fields["path"] == "/healthz"
    assert records[0].fields["status"] == 200
    assert records[0].fields["duration_ms"] >= 0
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert 'http_requests_total{path="/healthz",status="200"} 1.0' in response.text
    assert 'http_requests_total{path="/api/hollows",status="403"} 1.0' in response.text
    assert 'http_requests_total{path="unmatched",status="404"} 1.0' in response.text
    assert 'http_request_duration_seconds_bucket{le=' in response.text


@pytest.mark.parametrize("dependencies", [("postgres",), ("redis",), ("postgres", "redis")])
def test_configured_outage_keeps_liveness(monkeypatch, dependencies):
    def postgres_down(*args, **kwargs):
        raise psycopg.OperationalError("unavailable")

    def redis_down(*args, **kwargs):
        raise redis.ConnectionError("unavailable")

    if "postgres" in dependencies:
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/urdnot")
        monkeypatch.setattr(psycopg, "connect", postgres_down)
    if "redis" in dependencies:
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setattr(redis.Redis, "ping", redis_down)
        monkeypatch.setattr(redis.Redis, "incr", redis_down)
    with TestClient(create_app()) as session:
        assert session.get("/healthz").status_code == 200
        assert session.get("/").status_code == 200
        response = session.get("/readyz")
        assert response.status_code == 503
        assert response.json()["failing_dependencies"] == list(dependencies)
        if "postgres" in dependencies:
            assert session.get("/api/hollows").status_code == 503
            assert session.post("/api/hollows", json={"name": "A", "text": "B"},
                                headers={"X-Clan-Secret": "shiagur"}).status_code == 503
        if "redis" in dependencies:
            assert session.get("/api/headbutts").status_code == 503
