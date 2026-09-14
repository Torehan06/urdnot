"""Application, optional backends, and request instrumentation."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel
import redis
from starlette.concurrency import run_in_threadpool


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "service": "urdnot-api",
            "event": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        # Backend exception messages can contain credentials; log only their type.
        if record.exc_info:
            entry["error_type"] = record.exc_info[0].__name__
        return json.dumps(entry)


def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "info").upper(), handlers=[handler], force=True
    )
    # Request access records remain enabled even at a higher diagnostic LOG_LEVEL.
    logging.getLogger("urdnot-api.access").setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


log = logging.getLogger("urdnot-api")
access_log = logging.getLogger("urdnot-api.access")


class RequestMetrics:
    """ASGI middleware measures the full response and logs once, including errors."""

    def __init__(self, app, counter, latency):
        self.app, self.counter, self.latency = app, counter, latency

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        status = 500

        async def measured_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            duration = time.perf_counter() - started
            route = scope.get("route")
            # Avoid unbounded metric labels from arbitrary 404 URLs.
            path = route.path if route else "unmatched"
            self.counter.labels(path=path, status=str(status)).inc()
            self.latency.labels(path=path).observe(duration)
            access_log.info("request", extra={"fields": {
                "method": scope["method"], "path": scope["path"],
                "status": status, "duration_ms": round(duration * 1000, 3),
            }})


class HollowInput(BaseModel):
    name: str
    text: str


def create_app():
    database_url = os.getenv("DATABASE_URL") or None
    redis_url = os.getenv("REDIS_URL") or None
    storage_mode = "postgres" if database_url else "in-memory"
    counter_mode = "redis" if redis_url else "in-process"
    records = {}
    headbutts = 0
    lock = threading.Lock()
    schema_lock = threading.Lock()
    schema_ready = False
    redis_client = None
    started = time.monotonic()
    registry = CollectorRegistry()
    requests = Counter(
        "http_requests_total", "Completed HTTP requests", ["path", "status"],
        registry=registry,
    )
    latency = Histogram(
        "http_request_duration_seconds", "Full HTTP request duration", ["path"],
        registry=registry,
    )

    def connect():
        return psycopg.connect(
            database_url, connect_timeout=2, row_factory=dict_row,
            options="-c statement_timeout=2000 -c lock_timeout=2000",
        )

    def ensure_schema():
        nonlocal schema_ready
        with schema_lock:
            if not schema_ready:
                with connect() as conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS hollows (
                            id UUID PRIMARY KEY,
                            name TEXT NOT NULL,
                            text TEXT NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL
                        )
                    """)
                schema_ready = True

    def get_redis():
        nonlocal redis_client
        with lock:
            if redis_client is None:
                redis_client = redis.Redis.from_url(
                    redis_url, socket_connect_timeout=2, socket_timeout=2,
                    retry=redis.retry.Retry(redis.backoff.NoBackoff(), 0),
                )
            return redis_client

    @asynccontextmanager
    async def lifespan(application):
        nonlocal started
        started = time.monotonic()
        log.info("startup", extra={"fields": {
            "storage_mode": storage_mode, "counter_mode": counter_mode,
        }})
        if database_url:
            try:
                await run_in_threadpool(ensure_schema)
            except (psycopg.Error, ValueError):
                log.warning("dependency_unavailable", extra={"fields": {
                    "dependency": "postgres",
                }})
        try:
            yield
        finally:
            if redis_client is not None:
                await run_in_threadpool(redis_client.close)
            log.info("shutdown")

    application = FastAPI(title="urdnot-api", lifespan=lifespan)
    application.add_middleware(RequestMetrics, counter=requests, latency=latency)

    @application.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @application.get("/readyz")
    def readyz():
        failures = []
        if database_url:
            try:
                # Retry table creation if Postgres was unavailable at startup.
                ensure_schema()
                with connect() as conn:
                    conn.execute("SELECT 1")
            except (psycopg.Error, ValueError):
                failures.append("postgres")
        if redis_url:
            try:
                get_redis().ping()
            except (redis.RedisError, ValueError):
                failures.append("redis")
        if failures:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "failing_dependencies": failures},
            )
        return {"status": "ready"}

    @application.get("/metrics")
    def metrics():
        return Response(generate_latest(registry), headers={
            "Content-Type": CONTENT_TYPE_LATEST,
        })

    @application.get("/", response_class=HTMLResponse)
    async def index():
        clan = escape(os.getenv("CLAN_NAME", "Urdnot"))
        hostname = escape(socket.gethostname())
        version = escape(os.getenv("APP_VERSION", "0.1.0"))
        uptime = time.monotonic() - started
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>urdnot-api</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui;max-width:48rem;margin:3rem auto;padding:1rem">
<h1>{clan}</h1><p>Serving from hostname</p>
<strong style="font-size:clamp(2rem,6vw,4rem);overflow-wrap:anywhere">{hostname}</strong>
<p>Service: urdnot-api · Version: {version}</p>
<p>Uptime: {uptime:.1f} seconds</p>
<p>Storage: {storage_mode} · Counter: {counter_mode}</p>
</body></html>"""

    @application.get("/api/hollows")
    def list_hollows():
        if database_url:
            try:
                ensure_schema()
                with connect() as conn:
                    return conn.execute(
                        "SELECT id, name, text, created_at FROM hollows "
                        "ORDER BY created_at, id"
                    ).fetchall()
            except (psycopg.Error, ValueError):
                raise HTTPException(503, detail={"dependency": "postgres"}) from None
        with lock:
            return list(records.values())

    @application.post("/api/hollows", status_code=201)
    def create_hollow(item: HollowInput, x_clan_secret: str | None = Header(None)):
        expected = os.getenv("CLAN_SECRET", "shiagur")
        if x_clan_secret is None or not secrets.compare_digest(
            x_clan_secret.encode(), expected.encode()
        ):
            raise HTTPException(403, detail="Invalid clan secret")
        record = {
            "id": str(uuid4()), "name": item.name, "text": item.text,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if database_url:
            try:
                ensure_schema()
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO hollows (id, name, text, created_at) "
                        "VALUES (%(id)s, %(name)s, %(text)s, %(created_at)s)", record,
                    )
            except (psycopg.Error, ValueError):
                raise HTTPException(503, detail={"dependency": "postgres"}) from None
        else:
            with lock:
                records[record["id"]] = record
        return record

    @application.get("/api/headbutts")
    def count_headbutts():
        nonlocal headbutts
        if redis_url:
            try:
                return {"count": get_redis().incr("urdnot-api:headbutts")}
            except (redis.RedisError, ValueError):
                raise HTTPException(503, detail={"dependency": "redis"}) from None
        with lock:
            headbutts += 1
            return {"count": headbutts}

    @application.get("/api/charge")
    def charge(ms: int = Query(200, ge=1, le=10000)):
        wall_start = time.perf_counter()
        cpu_start = time.thread_time()
        value = 1
        # A sync handler runs in the thread pool, keeping the event loop responsive.
        # Thread CPU time ensures concurrent requests each consume their own budget.
        while (time.thread_time() - cpu_start) * 1000 < ms:
            value = (value * 1664525 + 1013904223) & 0xFFFFFFFF
        return {
            "requested_ms": ms,
            "duration_ms": round((time.perf_counter() - wall_start) * 1000, 3),
            "cpu_ms": round((time.thread_time() - cpu_start) * 1000, 3),
        }

    return application


app = create_app()
