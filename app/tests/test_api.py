from datetime import datetime
import logging
import socket
import time

from fastapi.testclient import TestClient
import psycopg
import pytest
import redis

from app.main import create_app


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    for key in ("DATABASE_URL", "REDIS_URL", "CLAN_NAME", "CLAN_SECRET",
                "CLAN_SECRET_FILE", "APP_VERSION", "APP_REVISION",
                "SESSION_TTL_SECONDS"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def client():
    with TestClient(create_app()) as session:
        yield session


def test_healthz(client):
    assert client.get("/healthz").status_code == 200


def test_home_shows_version_and_revision(client, monkeypatch):
    assert "Revision: unknown" in client.get("/").text
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    monkeypatch.setenv("APP_REVISION", "abc1234")
    page = client.get("/").text
    assert "Version: 9.9.9" in page
    assert "Revision: abc1234" in page


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


def test_session_lifecycle(client):
    assert client.get("/api/session").status_code == 401
    response = client.post("/api/session", json={"name": "Wrex"})
    assert response.status_code == 201
    assert response.json() == {"name": "Wrex", "visits": 0,
                               "served_by": socket.gethostname(), "store": "in-process"}
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Max-Age=3600" in cookie
    # TestClient keeps the cookie, like a browser.
    assert client.get("/api/session").json()["visits"] == 1
    assert client.get("/api/session").json()["visits"] == 2
    assert client.delete("/api/session").status_code == 204
    assert client.get("/api/session").status_code == 401


def test_session_rejects_unknown_token_and_bad_name(client):
    client.cookies.set("urdnot_session", "made-up")
    assert client.get("/api/session").status_code == 401
    assert client.post("/api/session", json={"name": ""}).status_code == 422


def test_in_process_sessions_are_not_shared_between_replicas():
    # Two app instances stand in for two replicas without Redis: the second
    # one has never heard of the session the first one created.
    with TestClient(create_app()) as first, TestClient(create_app()) as second:
        token = first.post("/api/session", json={"name": "Wrex"}).cookies["urdnot_session"]
        second.cookies.set("urdnot_session", token)
        assert first.get("/api/session").status_code == 200
        assert second.get("/api/session").status_code == 401


def test_session_expires(monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "1")
    with TestClient(create_app()) as session:
        session.post("/api/session", json={"name": "Wrex"})
        assert session.get("/api/session").status_code == 200
        now = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: now + 2)
        assert session.get("/api/session").status_code == 401


def test_session_ttl_must_be_positive(monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "0")
    with pytest.raises(ValueError):
        create_app()


class FakeRedis:
    """Enough of the Redis client for the session handlers, shared like a server."""

    def __init__(self):
        self.data, self.ttl, self.queued = {}, {}, []

    def pipeline(self):
        return self

    def hset(self, key, mapping):
        self.queued.append(lambda: self.data.setdefault(key, {}).update(
            {k: str(v).encode() for k, v in mapping.items()}))

    def hget(self, key, field):
        self.queued.append(lambda: self.data.get(key, {}).get(field))

    def hincrby(self, key, field, amount):
        def run():
            entry = self.data.setdefault(key, {})
            entry[field] = str(int(entry.get(field, b"0")) + amount).encode()
            return int(entry[field])
        self.queued.append(run)

    def expire(self, key, seconds):
        self.queued.append(lambda: self.ttl.__setitem__(key, seconds))

    def execute(self):
        queued, self.queued = self.queued, []
        return [command() for command in queued]

    def delete(self, key):
        self.data.pop(key, None)

    def close(self):
        pass


def test_redis_sessions_are_shared_between_replicas(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "90")
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fake)
    with TestClient(create_app()) as first, TestClient(create_app()) as second:
        created = first.post("/api/session", json={"name": "Wrex"})
        assert created.json()["store"] == "redis"
        token = created.cookies["urdnot_session"]
        second.cookies.set("urdnot_session", token)
        assert first.get("/api/session").json()["visits"] == 1
        assert second.get("/api/session").json() == {
            "name": "Wrex", "visits": 2,
            "served_by": socket.gethostname(), "store": "redis"}
        # Redis holds a hash of the token, never the token, and a TTL.
        (key,) = fake.data
        assert token not in key
        assert fake.ttl[key] == 90
        # An unknown token must not leave an empty session behind.
        second.cookies.set("urdnot_session", "made-up")
        assert second.get("/api/session").status_code == 401
        assert len(fake.data) == 1
        second.cookies.set("urdnot_session", token)
        assert second.delete("/api/session").status_code == 204
        assert first.get("/api/session").status_code == 401
        assert fake.data == {}


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


def test_clan_secret_file_is_reread(client, monkeypatch, tmp_path):
    secret_file = tmp_path / "clan-secret"
    secret_file.write_text("first\n")
    monkeypatch.setenv("CLAN_SECRET", "from-env")
    monkeypatch.setenv("CLAN_SECRET_FILE", str(secret_file))
    payload = {"name": "A", "text": "B"}

    def post(secret):
        return client.post("/api/hollows", json=payload,
                           headers={"X-Clan-Secret": secret}).status_code

    assert post("from-env") == 403  # the file wins over CLAN_SECRET
    assert post("first") == 201     # trailing newline ignored
    secret_file.write_text("second")
    assert post("first") == 403     # rotated without restarting the app
    assert post("second") == 201


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_clan_secret_file_fails_closed(client, monkeypatch, tmp_path, content):
    secret_file = tmp_path / "clan-secret"
    if content is not None:
        secret_file.write_text(content)
    monkeypatch.setenv("CLAN_SECRET_FILE", str(secret_file))
    response = client.post("/api/hollows", json={"name": "A", "text": "B"},
                           headers={"X-Clan-Secret": ""})
    assert response.status_code == 503
    assert response.json() == {"detail": {"dependency": "clan-secret"}}


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
        monkeypatch.setattr(redis.Redis, "pipeline", redis_down)
        monkeypatch.setattr(redis.Redis, "delete", redis_down)
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
            assert session.post("/api/session", json={"name": "A"}).status_code == 503
            session.cookies.set("urdnot_session", "any")
            assert session.get("/api/session").status_code == 503
            assert session.delete("/api/session").status_code == 503
