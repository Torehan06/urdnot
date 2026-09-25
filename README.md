# Urdnot

Urdnot is a small Python 3.12 FastAPI web service called `urdnot-api`.
It is the application layer for a 12-week DevOps curriculum that takes a service from bare Linux to Kubernetes with CI/CD and observability.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Listen address; loopback-only for use behind a reverse proxy. |
| `PORT` | `8000` | HTTP listen port. |
| `DATABASE_URL` | Unset | Postgres connection URL; unset or empty uses an in-memory dict. |
| `REDIS_URL` | Unset | Redis connection URL; unset or empty uses an in-process counter. |
| `APP_VERSION` | `0.1.0` | Version displayed on the home page. |
| `LOG_LEVEL` | `info` | Diagnostic log level; request access records always emit at info. |
| `CLAN_NAME` | `Urdnot` | Clan name displayed on the home page; read at request time. |
| `CLAN_SECRET` | `shiagur` | Required value of `X-Clan-Secret` when creating a record; read at request time. The default is for local development only; override it in any deployment. |

## Run locally

From the repository root, using Python 3.12:

```sh
python3.12 -m venv app/.venv
. app/.venv/bin/activate
python -m pip install -r app/requirements.txt
python -m app
```

Open [the home page](http://127.0.0.1:8000/). Its large hostname makes it easy to see which replica answered a request. No Postgres or Redis is needed to start the service or use every endpoint.

**Set `HOST=0.0.0.0` when running inside a container.** The default remains `127.0.0.1` for the initial Linux and reverse-proxy exercises. Use `python -m app` as the process entry point to apply the environment settings, JSON logging, and graceful shutdown behavior.

All configuration comes from environment variables. `CLAN_NAME` and `CLAN_SECRET` are looked up for each request, so no image rebuild is needed. Changing deployment or shell environment variables does not modify an already running process's environment; restart or roll out the process with the new values.

## Endpoints

The light naming theme comes from the krogan clans in Mass Effect. The Hollows hold records of the dead; each record contains `id`, `name`, `text`, and a UTC `created_at` timestamp.

| Method and path | Behavior |
| --- | --- |
| `GET /` | Tiny HTML page with clan name, prominent hostname, version, uptime, storage mode, and counter mode. |
| `GET /healthz` | Always 200 when served; checks no dependencies. |
| `GET /readyz` | 200 when ready. Checks only configured backends; failures return 503 with `failing_dependencies`, e.g. `["postgres", "redis"]`. |
| `GET /metrics` | Prometheus exposition, including `http_requests_total{path,status}` and `http_request_duration_seconds{path}` histogram. |
| `GET /api/hollows` | List records. |
| `POST /api/hollows` | Create `{ "name": "...", "text": "..." }`; returns 201, or 403 for a missing/incorrect `X-Clan-Secret`. |
| `GET /api/headbutts` | Increment and return `{ "count": 1 }`; Redis uses `INCR urdnot-api:headbutts`. |
| `GET /api/charge?ms=200` | Burn CPU and return `requested_ms`, actual wall-clock `duration_ms`, and `cpu_ms`. Accepts 1–10000 ms; default 200. |

```sh
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS 'http://127.0.0.1:8000/api/charge?ms=200'
curl -fsS -X POST http://127.0.0.1:8000/api/hollows \
  -H 'Content-Type: application/json' \
  -H 'X-Clan-Secret: shiagur' \
  -d '{"name":"Shiagur","text":"Remembered in the Hollows."}'
curl -fsS http://127.0.0.1:8000/api/hollows
curl -fsS http://127.0.0.1:8000/api/headbutts
```

## Operating behavior

Startup logs identify the selected storage and counter modes. In-memory records and counters are per-process, reset on restart, and are not shared across replicas. Configured backends never silently fall back: their affected application endpoints return 503 during an outage, while liveness remains independent.

The `hollows` table is created on startup when Postgres is configured. If startup cannot reach Postgres, the process still starts; readiness and record requests retry initialization after recovery. Use a database role allowed to create this table. There is no migration framework.

Logs go to stdout as one JSON object per line. Each request produces one access record containing `method`, `path`, `status`, and `duration_ms`. Metric paths use route templates, with `unmatched` for unknown routes to bound label cardinality. Scrapes include completed requests; the current scrape is counted after its response. Run one service process per container so metrics and fallback state belong to that replica.

The charge handler runs real Python computation in a worker thread and measures its CPU budget with thread CPU time. The event loop remains available for health checks. Under concurrency or CPU throttling, wall time can exceed the requested CPU time; one Python process primarily saturates one core because of the GIL.

On SIGTERM, the entry point tells Uvicorn to stop accepting connections, waits for in-flight requests and application cleanup, and exits with status 0. Allow enough termination grace time for the active workload.

The repository contains the application and its documentation. The completed Ubuntu, Bash, Nginx, SSH, DNS, and firewall lab configuration is documented in [infra/week1](infra/week1/README.md). The `app/` directory holds the service; `infra/` holds server and deployment configuration, one folder per curriculum week. Compose with Postgres and Redis, CI/CD, Kubernetes, autoscaling, and monitoring infrastructure remain later curriculum exercises.

## Run in a container

```sh
docker build -t urdnot-api:0.1.0 .
docker run --rm -p 8000:8000 --stop-timeout 30 -e CLAN_SECRET="$(openssl rand -hex 16)" urdnot-api:0.1.0
```

The image runs as a non-root user, listens on `0.0.0.0:8000`, and reports health via `/healthz`. See [docs/week-02.md](docs/week-02.md) for the design.

## Tests

`app/requirements.txt` holds runtime dependencies only; test tooling is in `app/requirements-dev.txt`. From the repository root with the virtual environment active:

```sh
python -m pip install -r app/requirements-dev.txt
python -m pytest app/tests -q
```

Tests clear backend environment variables and need no running Postgres or Redis. They cover fallback endpoints, authentication, request-time configuration, metrics/logging, CPU work, and simulated configured-backend outages.
