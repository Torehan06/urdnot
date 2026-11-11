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
| `APP_REVISION` | `unknown` | Git commit the image was built from, displayed on the home page. The Dockerfile sets it from the `REVISION` build argument. |
| `LOG_LEVEL` | `info` | Diagnostic log level; request access records always emit at info. |
| `CLAN_NAME` | `Urdnot` | Clan name displayed on the home page; read at request time. |
| `CLAN_SECRET` | `shiagur` | Required value of `X-Clan-Secret` when creating a record; read at request time. The default is for local development only; override it in any deployment. |
| `CLAN_SECRET_FILE` | Unset | Path to a file holding the clan secret. When set, it takes precedence over `CLAN_SECRET` and is read on every request, so a rotated Kubernetes Secret volume takes effect without a restart. Surrounding whitespace is ignored. A missing, unreadable or empty file makes record creation return 503. |

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

All configuration comes from environment variables. `CLAN_NAME` and `CLAN_SECRET` are looked up for each request, so no image rebuild is needed. Changing deployment or shell environment variables does not modify an already running process's environment; restart or roll out the process with the new values. The one exception is `CLAN_SECRET_FILE`: the file is re-read on every request, so changing its contents takes effect immediately.

## Endpoints

The light naming theme comes from the krogan clans in Mass Effect. The Hollows hold records of the dead; each record contains `id`, `name`, `text`, and a UTC `created_at` timestamp.

| Method and path | Behavior |
| --- | --- |
| `GET /` | Tiny HTML page with clan name, prominent hostname, version, revision, uptime, storage mode, and counter mode. |
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

The repository contains the application and its documentation. The completed Ubuntu, Bash, Nginx, SSH, DNS, and firewall lab configuration is documented in [infra/week1](infra/week1/README.md). The `app/` directory holds the service; `infra/` holds server and deployment configuration, one folder per curriculum week. [`compose.yaml`](compose.yaml) runs the service with Postgres and Redis (Week 3). [`.github/workflows/ci.yml`](.github/workflows/ci.yml) tests every push and pull request and publishes the image on `main` (Week 4). [`infra/week5`](infra/week5/README.md) deploys the service, Postgres, and Redis to Kubernetes (Week 5). [`infra/week6`](infra/week6/README.md) is an overlay on top of it: an Ingress for `urdnot.localhost` and a clan secret mounted as a file that can be rotated without a restart (Week 6). [`infra/week7`](infra/week7/README.md) packages all of it as a Helm chart with a values file per environment (Week 7). [`infra/week8`](infra/week8/README.md) and [`scripts/deploy.sh`](scripts/deploy.sh) deploy a published image with `helm upgrade`, by hand or from the pipeline (Week 8). Autoscaling and monitoring infrastructure remain later curriculum exercises.

## Run in a container

```sh
docker build -t urdnot-api:0.1.0 .
docker run --rm -p 8000:8000 --stop-timeout 30 -e CLAN_SECRET="$(openssl rand -hex 16)" urdnot-api:0.1.0
```

The image runs as a non-root user, listens on `0.0.0.0:8000`, and reports health via `/healthz`. See [docs/week-02.md](docs/week-02.md) for the design.

## Run with Postgres and Redis (Compose)

```sh
cp .env.example .env    # replace every value, e.g. with: openssl rand -hex 24
docker compose up -d --build --wait
curl -fsS localhost:8000/readyz
docker compose down     # keeps data; `down -v` also deletes the pgdata and redisdata volumes
```

`.env` is git-ignored and only feeds Compose interpolation. `POSTGRES_PASSWORD` (required, URL-safe, because it is placed inside `DATABASE_URL`) and `CLAN_SECRET` (required) must be set. `CLAN_NAME`, `LOG_LEVEL`, and `REVISION` (image label) are optional. Compose sets the service's `DATABASE_URL` and `REDIS_URL` itself. Only the API is published, on `127.0.0.1:8000`. Postgres and Redis are reachable only on the Compose network. See [docs/week-03.md](docs/week-03.md) for the design.

## Run on Kubernetes

```sh
kubectl config current-context    # make sure this is the cluster you mean
{ echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"; echo "CLAN_SECRET=$(openssl rand -hex 24)"; } > infra/week5/secrets.env
kubectl apply -k infra/week5
kubectl -n urdnot rollout status deployment/urdnot-api
kubectl -n urdnot port-forward svc/urdnot-api 8000:80
```

`infra/week5/secrets.env` is git-ignored and becomes the `urdnot-secrets` Secret. The API runs as 2 replicas of the published `ghcr.io/torehan06/urdnot-api` image, pinned by tag and digest. Postgres and Redis are single-replica StatefulSets with PersistentVolumeClaims. Liveness probes use `/healthz` and readiness probes use `/readyz`. See [infra/week5/README.md](infra/week5/README.md) for the runbook and [docs/week-05.md](docs/week-05.md) for the design.

`kubectl apply -k infra/week6` deploys the same stack plus an Ingress for `http://urdnot.localhost` and mounts the clan secret as a file, so it can be rotated without a restart. It needs an Ingress controller and an image that supports `CLAN_SECRET_FILE`. See [infra/week6/README.md](infra/week6/README.md) and [docs/week-06.md](docs/week-06.md).

## Run with Helm

```sh
cp infra/week7/values-local.yaml.example infra/week7/values-local.yaml    # replace both secrets
helm upgrade --install urdnot infra/week7/urdnot -n urdnot-dev --create-namespace \
  -f infra/week7/values-dev.yaml -f infra/week7/values-local.yaml --wait
curl -fsS http://dev.urdnot.localhost/healthz
```

The chart in [`infra/week7/urdnot`](infra/week7/urdnot) deploys the same objects as `infra/week6`. `values-dev.yaml` (1 replica, debug logging) and `values-staging.yaml` (2 replicas) each install a separate environment into its own namespace. `values-local.yaml` is git-ignored and holds the secrets. See [infra/week7/README.md](infra/week7/README.md) for the runbook and [docs/week-07.md](docs/week-07.md) for the design.

## Tests

`app/requirements.txt` holds runtime dependencies only; test tooling is in `app/requirements-dev.txt`. From the repository root with the virtual environment active:

```sh
python -m pip install -r app/requirements-dev.txt
python -m pytest app/tests -q
```

Tests clear backend environment variables and need no running Postgres or Redis. They cover fallback endpoints, authentication, request-time configuration, metrics/logging, CPU work, and simulated configured-backend outages.

## CI/CD

GitHub Actions runs [`.github/workflows/ci.yml`](.github/workflows/ci.yml) on every push to `main` and on every pull request. `test` runs the unit tests. `smoke` builds the image, starts the Compose stack with a generated `.env`, and runs [`scripts/smoke-test.sh`](scripts/smoke-test.sh) against it. `k8s` lints the Helm chart in [`infra/week7`](infra/week7) and rehearses a deployment on a kind cluster: it installs the previous release, upgrades to the freshly built image with [`scripts/deploy.sh`](scripts/deploy.sh) while [`scripts/rollout-probe.sh`](scripts/rollout-probe.sh) counts lost requests (there must be none), runs the smoke test through a port-forward, and checks that a broken image is rolled back. On `main`, after all three pass, `publish` pushes `ghcr.io/torehan06/urdnot-api:sha-<commit>` and `:main` for `linux/amd64` and `linux/arm64`, and `deploy` then upgrades a test cluster to exactly that image, again with zero lost requests. See [docs/week-08.md](docs/week-08.md). Run the same smoke test locally against a running stack:

```sh
set -a; . ./.env; set +a
scripts/smoke-test.sh          # BASE_URL defaults to http://127.0.0.1:8000
```

See [docs/week-04.md](docs/week-04.md), [docs/week-05.md](docs/week-05.md) and [docs/week-07.md](docs/week-07.md) for the design.
