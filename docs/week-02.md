# Week 2: Containerizing urdnot-api with a multi-stage build

Week 2 packages the service as an OCI image that is small, runs as a non-root user, reports its health, and stops gracefully. Files: [`Dockerfile`](../Dockerfile), [`.dockerignore`](../.dockerignore), and a split of [`app/requirements.txt`](../app/requirements.txt) into runtime and [`app/requirements-dev.txt`](../app/requirements-dev.txt) test dependencies.

## What was built

**Starting point, from reading the repository first.**

- The app is FastAPI, in `app/main.py`. The entry point is `python -m app` (`app/__main__.py`), which configures JSON logging, reads `HOST` and `PORT`, and runs one Uvicorn server whose SIGTERM handler drains requests and exits 0.
- `GET /healthz` already existed. It returns `{"status": "ok"}` and checks no dependencies. `GET /readyz` checks Postgres and Redis only when they are configured. No endpoint was added: the existing one is already machine-readable, which is the right contract for a probe.
- Dependencies were a single, fully pinned `app/requirements.txt` with 25 packages. There is no `pyproject.toml` or lockfile. Eight of those packages were test-only: pytest and its dependencies, and httpx/httpcore/certifi for FastAPI's `TestClient`.
- `HOST` defaults to `127.0.0.1`, which is correct behind Week 1's Nginx but unreachable from outside a container.

**Changes.**

| File | Change |
| --- | --- |
| `app/requirements.txt` | Now the 17 runtime dependencies only. |
| `app/requirements-dev.txt` | New. `-r requirements.txt` plus the 8 test packages. All 25 pins are unchanged, only moved. |
| `Dockerfile` | Two stages. `builder` creates a venv and installs dependencies. `runtime` gets the venv and the code. |
| `.dockerignore` | An allowlist: only `app/` enters the build context, minus tests and caches. |
| `README.md` | Adds a "Run in a container" section, and the Tests section now installs `requirements-dev.txt`. |

## Why each Dockerfile choice was made

- **Pin the base image by tag and digest (`python:3.12.14-slim-trixie@sha256:2f17…`).** The tag says what it is: Python 3.12.14 on Debian 13. The digest guarantees the same bytes on every build, because tags can be re-pushed. It is a multi-arch index digest, so it resolves to arm64 on the Mac and amd64 on CI. The cost is manual bumps; Dependabot or Renovate should own that later.
- **Use the slim variant, not full or alpine.** The full `python` image adds about 1.4 GB of compilers and headers we don't need. Alpine uses musl, which breaks the manylinux wheels (`psycopg-binary`, `pydantic_core`) and can force source builds.
- **Put the same `ARG PYTHON_IMAGE` in both stages.** The venv's `python` symlinks must point to the same interpreter path and version in both stages, or it breaks at runtime.
- **Install dependencies into a venv at `/opt/venv`.** A venv is a single directory, so it can be copied between stages. It also keeps our packages separate from the base image's `site-packages`.
- **Create the venv with `--without-pip` and install with the builder's `pip --python`.** The runtime venv contains no package installer, which removes one tool an attacker could use and one CVE source.
- **Copy `requirements.txt` before any code.** Docker caches layer by layer. Changing `main.py` then reuses the cached install layer, which was verified below.
- **Use `RUN --mount=type=cache` for pip's cache.** Wheels are reused between builds on the same machine but never written into a layer. This replaces the old `--no-cache-dir` trade-off.
- **Pass `--only-binary=:all:`.** The build fails loudly instead of trying to compile from source. That is why the builder needs no `gcc` or `libpq-dev`. It is also why multi-stage saves less here than it would for a compiled app (see sizes).
- **Split runtime and dev requirements.** pytest does not belong in a production image. It adds size, attack surface, and scanner noise.
- **Create a system user with a fixed UID/GID 10001 and switch to it with a numeric `USER 10001:10001`.** If the container is compromised, the attacker is not root. A numeric UID lets Kubernetes' `runAsNonRoot` check it without reading `/etc/passwd`. A high number avoids collisions with host UIDs on bind mounts.
- **Keep code and venv owned by root (no `--chown`).** The service user can read and execute its code but cannot rewrite it. This was verified below.
- **Set `ENV HOST=0.0.0.0 PORT=8000`.** Inside a container, loopback is only reachable from inside the container. The network boundary is now Docker's port publishing, not the bind address.
- **Set `PYTHONUNBUFFERED=1` and `PYTHONDONTWRITEBYTECODE=1`.** JSON logs reach `docker logs` immediately. Python does not try to write `.pyc` files into a read-only code directory; pip already precompiled the venv.
- **Put the OCI labels (`source`, `description`, `title`, `licenses`, `version`, `revision`) last.** GHCR uses `source` to link an image to its repository. `revision` ties an image to a commit. These come from `ARG`s. **This was a real bug caught during verification:** with the `ARG`s at the top of the stage, a new `REVISION` value rebuilt the `useradd` layer, because an `ARG` changes the cache key of every later `RUN`. They now come after the last `RUN`.
- **Write `HEALTHCHECK` as a Python one-liner, not curl.** The slim image has no curl, and adding it (plus libcurl) for a probe is not worth it. The probe reads `$PORT` at probe time. `--start-interval=1s` reports healthy about 1 s after start instead of waiting the full 30 s interval.
- **Probe `/healthz`, not `/readyz`.** Plain Docker only reports unhealthy; Swarm and other orchestrators act on it. A Postgres outage must not look like a dead process (see Week 1).
- **Use exec-form `CMD ["python", "-m", "app"]`.** With no shell wrapper, Python is PID 1 and receives SIGTERM from `docker stop` directly. Shell form (`CMD python -m app`) makes `/bin/sh` PID 1. sh doesn't forward signals, so the app gets SIGKILLed after the timeout. The naive build triggered Docker's `JSONArgsRecommended` warning for exactly this reason.
- **"Production server": one Uvicorn process per container, through the existing entry point.** The README requires one process per container so metrics and in-memory state belong to one replica. Scaling happens with replicas, not gunicorn workers. Using `python -m app` keeps the tested graceful-shutdown and JSON-logging code path. `uvicorn[standard]` (uvloop/httptools) was *not* added: it's a performance choice that should be driven by measurements.
- **Use no init process (`tini`).** The app spawns no child processes, so it has no zombies to reap, and it handles SIGTERM itself. If that changes, run with `docker run --init`.
- **Use an allowlist `.dockerignore`.** The context is `app/` only. A future `.env`, `.git`, or a venv can't leak into the image or bust the cache by accident.
- **Do not bake in `CLAN_SECRET`.** Secrets are runtime configuration. The app's built-in default, `shiagur`, still applies if you forget, so always pass `-e CLAN_SECRET=…`.

## Size comparison

Three images were built from the same code and the same 17 runtime dependencies. The two baselines were temporary and were removed afterward.

```
IMAGE                    DISK(unpacked)   COMPRESSED
urdnot-api:naive              1.7GB        440MB     # FROM python:3.12.14-trixie; COPY . .; pip install; root; shell-form CMD
urdnot-api:slim-single        276MB         72MB     # same pinned slim base, one stage, COPY . .
urdnot-api:0.1.0              237MB         57MB     # this Dockerfile
```

"Compressed" is what a registry stores and a node pulls. "Disk" is the unpacked size on the host.

**How to read this honestly:** nearly all of the 1.7 GB → 276 MB win comes from the **base image choice**, not from multi-stage. Multi-stage then removes a further ~39 MB on disk (15 MB compressed): pip's download cache (13 MB, measured in `slim-single`), `.git`, the tests, and pip inside the venv. The base image is 179 MB of the final 237 MB. The venv is 45 MB, and most of it is `psycopg-binary`, which bundles libpq and OpenSSL. Multi-stage pays off much more when the builder needs a compiler toolchain. This build avoids that with wheels.

The layer history shows the only layers with real size are the venv and the code:

```
$ docker history urdnot-api:0.1.0 --format '{{.Size}}\t{{.CreatedBy}}'
0B      CMD ["python" "-m" "app"]
0B      HEALTHCHECK {Test:[CMD python -c import os, …
0B      EXPOSE [8000/tcp]
0B      USER 10001:10001
0B      ENV APP_VERSION=0.1.0
0B      LABEL org.opencontainers.image.title=urdnot-…
0B      ARG REVISION=ea99bf3
0B      ARG VERSION=0.1.0
41kB    COPY app/ ./app/ # buildkit
46.4MB  COPY /opt/venv /opt/venv # buildkit
12.3kB  WORKDIR /opt/urdnot
…       (base image layers)
```

## Verification

**Environment caveat:** this was verified in a **linux/amd64** cloud sandbox with Docker Engine 29.3.1, not on your MacBook. Two sandbox-only workarounds did not touch the Dockerfile. The sandbox needed `--network host --build-arg HTTPS_PROXY=…` to reach PyPI through its egress proxy. `HTTPS_PROXY` is a predefined build argument and is not recorded in image history; `docker history --no-trunc | grep -ci proxy` returned `0`. Docker Hub also rate-limited the shared IP (HTTP 429), so the full `python` image for the naive baseline was pulled from `mirror.gcr.io`, Google's Docker Hub mirror, which serves the same image. For arm64: all 17 pinned runtime dependencies resolve to arm64 or pure-Python wheels (`pip download --platform manylinux_2_28_aarch64 --only-binary=:all:` fetched 17 files), and the base digest includes `linux/arm64/v8`. **Rerun the commands below on the Mac** to confirm natively.

**Cache ordering.** After a one-line change to `app/__init__.py` and a new `REVISION`, only the code layer rebuilt:

```
#8  [builder 4/4] RUN --mount=type=cache,target=/root/.cache/pip ... pip install ...
#8  CACHED
#10 [runtime 2/5] RUN groupadd --system --gid 10001 urdnot  && useradd ...
#10 CACHED
#13 [runtime 4/5] COPY --from=builder /opt/venv /opt/venv
#13 CACHED
#14 [runtime 5/5] COPY app/ ./app/
#14 DONE 0.0s
```

**Run, probe, and check users:**

```
$ docker run -d --name urdnot -p 8000:8000 --stop-timeout 30 -e CLAN_SECRET=local-dev-only urdnot-api:0.1.0
$ curl -fsS localhost:8000/healthz
{"status":"ok"}
$ docker exec urdnot id
uid=10001(urdnot) gid=10001(urdnot) groups=10001(urdnot)
$ docker top urdnot -o pid,user,uid,cmd
PID                 USER                UID                 CMD
3273                10001               10001               python -m app
$ docker exec urdnot touch /opt/urdnot/app/x
touch: cannot touch '/opt/urdnot/app/x': Permission denied
$ docker exec urdnot cat /proc/1/cmdline
python -m app
```

**Health, as Docker reports it:**

```
$ docker inspect -f '{{.State.Health.Status}} {{json (index .State.Health.Log 0)}}' urdnot
healthy {"Start":"2026-09-25T12:27:54.416013966Z","End":"2026-09-25T12:27:54.80051963Z","ExitCode":0,"Output":""}
```

**Graceful stop with a request in flight.** A 3 s `/api/charge` request was started, and `docker stop` was sent 0.5 s later:

```
$ time docker stop urdnot
real    0m2.866s
{"requested_ms":3000,"duration_ms":3049.982,"cpu_ms":3000.025}     # the in-flight request completed
exit=0
{"event": "shutdown"} … {"event": "Finished server process [1]"}
```

**Labels:**

```
$ docker inspect -f '{{json .Config.Labels}}' urdnot-api:0.1.0
org.opencontainers.image.description  Krogan-themed FastAPI service for a 12-week DevOps curriculum
org.opencontainers.image.licenses     MIT
org.opencontainers.image.revision     ea99bf3
org.opencontainers.image.source       https://github.com/Torehan06/urdnot
org.opencontainers.image.title        urdnot-api
org.opencontainers.image.version      0.1.0
```

**Tests after the requirements split,** in `python:3.12.14-slim-trixie` with `requirements-dev.txt`: `17 passed, 2 warnings in 2.42s`.

**Vulnerability scan: skipped.** Neither `trivy` nor `docker scout` was installed in the build environment (`docker: unknown command: docker scout`). Both are free. On the Mac, run `docker scout cves urdnot-api:0.1.0` (bundled with Docker Desktop) or `brew install trivy && trivy image urdnot-api:0.1.0`. Expect mostly low-severity Debian base-image findings with no fix available. Those are handled by rebuilding on new base digests, not by patching in the Dockerfile.

## How to build and run it

```sh
# Build. REVISION and VERSION only affect labels and APP_VERSION.
docker build \
  --build-arg REVISION="$(git rev-parse --short HEAD)" \
  --build-arg VERSION=0.1.0 \
  -t urdnot-api:0.1.0 .

# Run. Always set a real CLAN_SECRET. --stop-timeout 30 matches Week 1's TimeoutStopSec.
docker run -d --name urdnot -p 8000:8000 --stop-timeout 30 \
  -e CLAN_SECRET="$(openssl rand -hex 16)" urdnot-api:0.1.0

curl -fsS localhost:8000/healthz
docker inspect -f '{{.State.Health.Status}}' urdnot
docker logs -f urdnot
docker stop urdnot && docker rm urdnot
```

To build for a platform other than your machine's, for example amd64 from the Mac: `docker buildx build --platform linux/amd64 …`.

## What you should be able to explain in an interview

- What a layer is, how the build cache decides reuse (instruction plus inputs, including `ARG` values and file checksums, not mtimes), and why dependency manifests are copied before code. Use the `ARG` placement bug as a concrete story.
- What multi-stage actually removes here, and why base-image choice was the bigger win. Don't overclaim.
- Why the image runs as a numeric non-root UID and the code is root-owned and read-only to that user. Explain what `runAsNonRoot` checks.
- PID 1 and signals: exec vs shell form, why the drain test proves it works, and when you'd add `tini` or `--init`.
- A liveness probe (`/healthz`) vs a readiness probe (`/readyz`), and why the Docker `HEALTHCHECK` uses liveness.
- Tag vs digest pinning, what a multi-arch index is, and how an image stays patched (automated base bumps plus rebuilds).
- Why secrets are runtime environment variables and not `ENV` in the Dockerfile. Anything in `ENV` or `ARG` can be read back with `docker history` or `docker inspect`.

## Test your understanding

1. You move `COPY app/ ./app/` above the `COPY --from=builder /opt/venv` line. Does the dependency install get slower on code changes? What *does* change, and why?
2. You change `CMD` to `CMD python -m app` and run `docker stop` during a 3 s `/api/charge` request. Predict how long `docker stop` takes, what exit code you'll see, and whether the request completes.
3. Someone "fixes" a health check failure by switching `HEALTHCHECK` to `/readyz`, then Redis goes down in Week 3. What happens to the container's health, and what would an orchestrator that restarts unhealthy containers do? Why is that worse than the current behavior?

## Notes for Week 3 (not built yet)

- **Postgres and Redis endpoints.** In Compose, set `DATABASE_URL` and `REDIS_URL` to the service names (`postgres`, `redis`), not `localhost`. The app already tolerates Postgres being down at startup, so `depends_on: condition: service_healthy` is a convenience, not a correctness requirement.
- **Probes in Compose.** Compose's own `healthcheck:` can override the image's check. Keep it on `/healthz`. Use `/readyz` to gate traffic, not restarts.
- **The `hollows` table.** It is created at startup, and the DB role needs `CREATE` privilege (README). There is still no migration tool.
- **Secrets.** Use a git-ignored `.env` file or Compose secrets for `CLAN_SECRET` and database passwords. Don't put them in the Compose file.
- **Test stage.** A `test` stage (`FROM builder` + `requirements-dev.txt` + `pytest`) would let CI run tests inside the same toolchain in Week 4. It was deliberately not added now.

## Assumptions made

- **Base image:** Debian 13 "trixie" slim, the current Debian stable, over bookworm, and Python 3.12 to match the README.
- **Build directory:** `/opt/urdnot` as `WORKDIR`, to mirror Week 1's `/opt/urdnot` layout.
- **Frontend pin:** `# syntax=docker/dockerfile:1` tracks the stable 1.x frontend. That is Docker's documented convention; it is a major-version channel, not `:latest`.
- **Supply chain:** `--require-hashes` (hash-pinned requirements) would be the next supply-chain step. It needs a lock tool such as `pip-compile --generate-hashes` or `uv`, so it was left for later.
