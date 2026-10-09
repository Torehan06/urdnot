# Week 3: Running urdnot-api with Postgres and Redis in Compose

Week 3 runs the Week 2 image next to its real backends. Records go to Postgres, the headbutt counter goes to Redis, and one command brings the stack up. Files: [`compose.yaml`](../compose.yaml) and [`.env.example`](../.env.example), plus a Compose section in the [README](../README.md). No application code changed.

## What was built

**Starting point.** The app already supported both backends through `DATABASE_URL` and `REDIS_URL`. It creates the `hollows` table on startup, tolerates Postgres being down at startup, and returns 503 (never a silent fallback) when a configured backend is down. Week 2's notes listed what Compose had to respect: service names instead of `localhost`, health checks on `/healthz` only, and secrets outside the Compose file.

| Service | Image | Published | Storage |
| --- | --- | --- | --- |
| `api` | `urdnot-api:0.1.0`, built from the Week 2 `Dockerfile` | `127.0.0.1:8000` | none (stateless) |
| `postgres` | `postgres:18.6-trixie@sha256:5a5a…9722` | no | named volume `pgdata` → `/var/lib/postgresql` |
| `redis` | `redis:8.10.2-trixie@sha256:6f81…50ee` | no | named volume `redisdata` → `/data` |

Compose names the project `urdnot`, so the containers are `urdnot-api-1` and so on, the network is `urdnot_default`, and the volumes are `urdnot_pgdata` and `urdnot_redisdata`.

## Why each choice was made

- **Put `compose.yaml` at the repo root, not in `infra/week3/`.** Its build context is the repo root (where the `Dockerfile` is), and `docker compose up` finds `compose.yaml` in the current directory. Later weeks (CI in Week 4) reuse it, so it is project-level config like the `Dockerfile`, not a one-week lab artifact. `compose.yaml` is the current canonical filename; `docker-compose.yml` is the legacy name.
- **Use service names as hostnames (`@postgres:5432`, `redis://redis:6379`).** Compose puts all services on one user-defined network with built-in DNS. Inside the `api` container, `localhost` is the `api` container itself, not the Mac and not Postgres.
- **Publish only the API, and only on loopback (`127.0.0.1:8000:8000`).** Postgres and Redis have no `ports:`, so nothing outside the Compose network can reach them. Writing `"8000:8000"` would bind `0.0.0.0` and expose the API to the LAN. This mirrors Week 1, where only Nginx faced the network. Because of this isolation, Redis runs without AUTH. On a shared host or in production it would get a password or ACLs.
- **Pin both backend images by tag and digest, like the Python base in Week 2.** Postgres 18.6 is the newest stable release (19 is still in beta). Both images use Debian 13 "trixie", the same as the app image. That gives one OS family to track for CVEs and lets the images share base layers.
- **Mount the Postgres volume at `/var/lib/postgresql`, not `/var/lib/postgresql/data`.** Since Postgres 18, the official image sets `PGDATA=/var/lib/postgresql/18/docker` and declares the volume one level up (checked with `docker image inspect`). The old path, still in many tutorials, would leave the real data in an anonymous volume, and that data would not survive `down`. The new layout also lets a future `pg_upgrade` keep `17/` and `18/` side by side.
- **Use named volumes, not bind mounts.** Docker manages them, they survive `docker compose down`, and they avoid UID and permission mismatches between macOS and the container's `postgres` user. Only `docker compose down -v` deletes them.
- **Give Redis a volume too.** The counter is not critical, but the default RDB snapshot is written to `/data` on shutdown, so the count survives restarts. This was verified below. Without the volume, every `down`/`up` would reset it, a difference that would be easy to miss.
- **Health checks that tell the truth.**
  - Postgres: `pg_isready -h 127.0.0.1`. During first-boot init, the entrypoint runs a temporary server that listens only on the Unix socket and then restarts it. A socket-based check could report healthy during that window. The TCP check avoids it. `$$POSTGRES_USER` escapes Compose interpolation, so the shell inside the container expands it.
  - Redis: `redis-cli ping | grep -q PONG`. `redis-cli` exits 0 even on an error reply (verified: `ERR unknown command` → exit 0), so `-LOADING` during a large snapshot load would otherwise count as healthy.
  - API: no override. The image's `HEALTHCHECK` already probes `/healthz` (liveness). Readiness (`/readyz`) is never used for container health; see the outage test below.
  - `start_interval: 1s` probes quickly during `start_period`, so `depends_on` releases the API about 2 s after start instead of after the first 10 s interval.
- **Use `depends_on: condition: service_healthy` as a convenience only.** It avoids a burst of 503s on a cold start. Correctness does not depend on it, because the app retries table creation when Postgres comes back. `depends_on` only controls start order. It does nothing when a dependency dies later.
- **Set `restart: on-failure`, the same as Week 1's `Restart=on-failure`.** Crashes are restarted. A clean exit (status 0 after SIGTERM) and a manual `docker compose stop` are not. `unless-stopped` was rejected: on Docker Desktop it would bring the stack back every time Docker starts. Plain Docker does **not** restart *unhealthy* containers. That is an orchestrator feature (Swarm, or Kubernetes liveness probes in a later week).
- **Set `stop_grace_period: 30s`, the same as Week 1's `TimeoutStopSec=30`.** Docker's default is 10 s, which equals the longest `/api/charge`, leaving no margin. Postgres keeps its image's `StopSignal SIGINT` (Postgres "fast shutdown"); Compose uses each image's own stop signal.
- **Secrets: a git-ignored `.env` plus required interpolation.** `.env` was already in `.gitignore`, and the `.dockerignore` allowlist keeps it out of the build context. `${POSTGRES_PASSWORD:?…}` makes `docker compose config` and `up` fail with a clear message if a value is missing, instead of starting with an empty password or the public default `CLAN_SECRET`. `.env.example` documents the variables without values. The password must be URL-safe, because it is placed inside `DATABASE_URL`.
  - **Alternative rejected: Compose `secrets:`.** Postgres can read `POSTGRES_PASSWORD_FILE`, but the app takes the password inside `DATABASE_URL` and has no `*_FILE` support. The libpq route (`PGPASSFILE`) needs a mode-0600 file readable by UID 10001. Compose (non-Swarm) secrets are bind mounts that keep the host file's ownership, so this becomes fiddly on macOS. Adding file-based secrets to the app is a reasonable later change (Kubernetes Secrets can be mounted either way).
  - **Known cost.** Environment variables are visible to anyone who can run `docker inspect urdnot-api-1` (verified: `DATABASE_URL` and `CLAN_SECRET` appear in plain text). They are not in the image or in git, but on this host, Docker access means secret access.
- **The build is in the Compose file, so `--build` rebuilds the image.** `image: urdnot-api:0.1.0` names the result, which is the same tag as Week 2. `REVISION` comes from `.env` (optional) and only affects the OCI label.

## Verification

Run on 2026-10-05 on the MacBook Air (arm64), Docker Desktop with Engine 29.4.1 and Compose v5.1.3, from branch `week-03-compose` (based on `83c9d2d`). `.env` was generated with `openssl rand -hex`.

**Config fails loudly without secrets:**

```
$ docker compose config -q      # no .env yet
error while interpolating services.api.environment.CLAN_SECRET: required variable CLAN_SECRET is missing a value: set CLAN_SECRET in .env
```

**Start order.** The cold start with an existing image took 8 s. The API was created first but started only after both backends were healthy:

```
 Container urdnot-redis-1 Healthy
 Container urdnot-postgres-1 Healthy
 Container urdnot-api-1 Starting
SERVICE    STATUS                          PORTS
api        Up 10 seconds (healthy)         127.0.0.1:8000->8000/tcp
postgres   Up 13 seconds (healthy)         5432/tcp
redis      Up 13 seconds (healthy)         6379/tcp
```

**The real backends are used:**

```
{"event": "startup", "storage_mode": "postgres", "counter_mode": "redis"}
$ curl -fsS localhost:8000/readyz                         → {"status":"ready"}
POST /api/hollows with no secret                          → 403
POST /api/hollows with the public default "shiagur"       → 403   # .env value is in effect
POST /api/hollows with $CLAN_SECRET                       → 201 {"id":"3e6d8f04-…","name":"Shiagur",…}
GET /api/headbutts ×2                                     → {"count":1} {"count":2}
$ docker compose exec postgres psql -U urdnot -d urdnot -c 'SELECT name, created_at FROM hollows;'
 Shiagur | 2026-10-05 09:57:34.557873+00
$ docker compose exec redis redis-cli GET urdnot-api:headbutts
2
```

**Persistence across `down` and `up`.** `down` removed the containers and the network but kept the volumes. The record was still there, and the counter continued from 2:

```
$ docker compose down && docker compose up -d --wait
$ curl -fsS localhost:8000/api/hollows    → [{"id":"3e6d8f04-…","name":"Shiagur",…}]
$ curl -fsS localhost:8000/api/headbutts  → {"count":3}
```

**Dependency outage (Week 2's Question 3, answered by experiment).** Redis was stopped, then Postgres, and the stack was left down for 35 s so the image `HEALTHCHECK` ran during the outage:

```
redis stopped:     /readyz 503 {"failing_dependencies":["redis"]}   /api/headbutts 503   /healthz 200   /api/hollows 200
postgres stopped:  /readyz 503 {"failing_dependencies":["postgres","redis"]}   /api/hollows 503
after 35 s:        api running Up 37 seconds (healthy)   restarts=0 failingStreak=0
restarted both:    /readyz 200 {"status":"ready"}   /api/headbutts {"count":4}
```

Liveness stayed green, so nothing would restart the API for a problem a restart can't fix. Readiness reported exactly which dependency was down, and the API recovered by itself, with no restart, when the backends returned.

**Network isolation:**

```
$ nc -z 127.0.0.1 5432   → closed from host
$ nc -z 127.0.0.1 6379   → closed from host
$ docker compose exec api python -c '…gethostbyname…'
api resolves postgres -> 172.18.0.2 redis -> 172.18.0.3
```

**Graceful stop through Compose.** A 3 s `/api/charge` was in flight when `docker compose stop api` ran:

```
real 2.89
{"requested_ms":3000,"duration_ms":3005.442,"cpu_ms":3000.06}  http=200, curl exit 0
exit=0   … "Application shutdown complete." … "Finished server process [1]"
```

**Unit tests are unaffected:** `python -m pytest app/tests -q` → `17 passed, 2 warnings in 2.73s` (the same two upstream deprecation warnings as Week 2).

**Volume sizes after the tests:** `urdnot_pgdata` 48.5 MB (mostly an empty cluster's catalog and WAL), `urdnot_redisdata` 118 B.

## How to run it

```sh
cp .env.example .env            # put real values in; openssl rand -hex 24
docker compose up -d --build --wait
docker compose ps
docker compose logs -f api
curl -fsS localhost:8000/readyz

docker compose exec postgres psql -U urdnot -d urdnot    # SQL shell
docker compose exec redis redis-cli                      # Redis shell

docker compose stop             # stop, keep containers
docker compose down             # remove containers + network, keep data
docker compose down -v          # also delete pgdata and redisdata (irreversible)
```

## Break it once (diagnose from logs before reading code)

In `compose.yaml`, change `@postgres:5432` in `DATABASE_URL` to `@localhost:5432`, then run `docker compose up -d`. Before you look anything up, answer these: Does the API container become healthy? What do `/readyz`, `/api/hollows`, and `/api/headbutts` return? Which log line tells you, and why does it name the dependency but not the error message? (Hint: read `JsonFormatter`.) Revert afterward.

## What you should be able to explain in an interview

- How containers in one Compose project find each other: the user-defined network, embedded DNS, and why `localhost` is wrong inside a container.
- `ports` vs no `ports`: what is reachable from the host, from the LAN, and from other containers, and why the backends publish nothing.
- Named volume vs bind mount vs anonymous volume, what `down` and `down -v` delete, and the Postgres 18 `PGDATA` path change as a concrete trap.
- What `depends_on: service_healthy` does and does not guarantee, and why the app must handle a dependency disappearing later anyway.
- Liveness vs readiness, demonstrated: the outage test above, and what would have happened if the health check used `/readyz`.
- Restart policies (`no`, `on-failure`, `unless-stopped`, `always`) and the fact that plain Docker never restarts an *unhealthy* container.
- Where the secrets live, who can read them (`docker inspect`), and what Compose `secrets:` would and would not improve.

## Test your understanding

1. You run `docker compose down -v` by habit, then `up`. What is lost, what is recreated, and why does the API still start cleanly with no migration step?
2. You add `ports: ["5432:5432"]` to Postgres "to debug with a GUI". What changed about who can reach the database, and what would be a safer way to get the same access? (Two options: loopback binding, or `docker compose exec`.)
3. Redis is stopped for an hour. Does the `restart: on-failure` policy bring it back? Does anything restart the API? What does a client of `/api/headbutts` see, and when does it stop seeing it?
4. You run `docker compose up -d --scale api=3`. What fails, why, and what would you need to put in front of the replicas? (The home page's hostname is there to show which replica answered.)

## Notes for Week 4 (not built yet)

- CI can run this same `compose.yaml` for an integration smoke test: `up --wait`, curl `/readyz`, POST and GET a record, then `down -v`. A CI `.env` would be generated per run.
- A `test` stage in the `Dockerfile` (Week 2's notes) is still not added.
- Base-image and backend-image digests now live in two files (`Dockerfile`, `compose.yaml`); Dependabot or Renovate can bump both.

## Assumptions made

- **Postgres 18 (stable), not 19 beta.** Debian trixie variants were chosen over Alpine for consistency with the app image. Alpine Postgres also has weaker locale and collation support (musl).
- **Database name and role are both `urdnot`.** The role created by `POSTGRES_USER` owns the database, so it can create the `hollows` table, as the README requires. A separate least-privilege app role would matter once a migration tool exists.
- **No resource limits (`cpus`, `mem_limit`).** They become meaningful with the Kubernetes and autoscaling weeks, where `/api/charge` is used against CPU limits.
- **No Nginx in front.** The Week 1 reverse proxy is not part of this stack; Kubernetes Ingress replaces it later.
