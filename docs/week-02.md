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
- **Create the venv with `--without-pip` and install with the builder's `pip --python`.** The runtime venv contains no package installer of its own. This is narrower than it sounds: the base image still ships its system `pip` in `/usr/local/lib/python3.12`, and that copy is where every Python finding in the vulnerability scan comes from (see Verification). Removing it would mean deleting it in the runtime stage or moving to a distroless-style base. That decision is left for later.
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

**Native arm64 build (MacBook Air, Docker Desktop 4.71.0, Engine 29.4.1).** The build was cold, with no base image and no pip cache present. It took **21 s** wall-clock, and pip downloaded `aarch64` manylinux wheels for all packages. A rebuild after a one-line code change took 2 s.

```
urdnot-api:0.1.0 (linux/arm64)
  unpacked (sum of layer sizes)   210 MB
  compressed content               57.9 MB
  Docker Desktop "DISK USAGE"      268 MB   # = unpacked + compressed blobs (containerd image store)
  venv layer                       51 MB    # 46.4 MB on amd64; aarch64 wheels are slightly larger
```

Docker Desktop uses the containerd image store, and its `docker images` "DISK USAGE" column counts both the unpacked snapshot and the compressed blobs it keeps. Use the 210 MB layer sum, not 268 MB, as the arm64 unpacked size. The compressed size is effectively the same on both architectures (57.9 MB vs 57 MB).

**Original amd64 comparison.** The first build ran in a linux/amd64 cloud sandbox (Docker Engine 29.3.1). Three images were built there from the same code and the same 17 runtime dependencies. The two baselines were temporary and were removed afterward. They were not rebuilt on arm64. The sandbox did not record how it measured "unpacked", so compare the compressed column across architectures, not the disk column.

```
IMAGE (amd64)            DISK(unpacked)   COMPRESSED
urdnot-api:naive              1.7GB        440MB     # FROM python:3.12.14-trixie; COPY . .; pip install; root; shell-form CMD
urdnot-api:slim-single        276MB         72MB     # same pinned slim base, one stage, COPY . .
urdnot-api:0.1.0              237MB         57MB     # this Dockerfile
```

"Compressed" is what a registry stores and a node pulls. "Disk" is the unpacked size on the host.

**How to read this honestly (amd64 numbers):** nearly all of the 1.7 GB → 276 MB win comes from the **base image choice**, not from multi-stage. Multi-stage then removes a further ~39 MB on disk (15 MB compressed): pip's download cache (13 MB, measured in `slim-single`), `.git`, the tests, and pip inside the venv. The base image is 179 MB of the final 237 MB. The venv is 45 MB, and most of it is `psycopg-binary`, which bundles libpq and OpenSSL. Multi-stage pays off much more when the builder needs a compiler toolchain. This build avoids that with wheels.

The layer history shows the only layers with real size are the venv and the code. This is the arm64 build:

```
$ docker history urdnot-api:0.1.0 --format '{{.Size}}\t{{.CreatedBy}}'
0B      CMD ["python" "-m" "app"]
0B      HEALTHCHECK {Test:[CMD python -c import os, …
0B      EXPOSE [8000/tcp]
0B      USER 10001:10001
0B      ENV APP_VERSION=0.1.0
0B      LABEL org.opencontainers.image.title=urdnot-…
0B      ARG REVISION=a20281b
0B      ARG VERSION=0.1.0
41kB    COPY app/ ./app/ # buildkit
51MB    COPY /opt/venv /opt/venv # buildkit
12.3kB  WORKDIR /opt/urdnot
0B      ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/l…
41kB    RUN /bin/sh -c groupadd --system --gid 10001…
…       (base image layers)
```

Inside the venv, `psycopg_binary` and its bundled `.libs` account for about 24 MB of the 49 MB.

## Verification

**Environment.** Everything below was verified natively on the target machine: a MacBook Air (Apple silicon, `uname -m` = `arm64`) running Docker Desktop 4.71.0 with Engine 29.4.1 on `linux/arm64`. The image was built from commit `a20281b` on 2026-09-25. The Dockerfile was first written and verified in a linux/amd64 cloud sandbox. Those results matched this run, except for the size numbers above and the vulnerability scan, which the sandbox could not run.

```
$ docker image inspect urdnot-api:0.1.0 -f 'arch={{.Architecture}} os={{.Os}}'
arch=arm64 os=linux
```

The build prints one harmless warning, `useradd warning: urdnot's uid 10001 is greater than SYS_UID_MAX 999`. `--system` normally picks a UID below 1000, and the Dockerfile overrides that on purpose (see the UID bullet above).

**Cache ordering.** After a one-line change to `app/__init__.py` and a new `REVISION`, only the code layer rebuilt. The rebuild took 2 s:

```
#8  [builder 3/4] COPY app/requirements.txt /tmp/requirements.txt
#8  CACHED
#9  [builder 4/4] RUN --mount=type=cache,target=/root/.cache/pip ... pip install ...
#9  CACHED
#10 [runtime 2/5] RUN groupadd --system --gid 10001 urdnot  && useradd ...
#10 CACHED
#11 [runtime 3/5] WORKDIR /opt/urdnot
#11 CACHED
#12 [builder 2/4] RUN python -m venv --without-pip /opt/venv
#12 CACHED
#13 [runtime 4/5] COPY --from=builder /opt/venv /opt/venv
#13 CACHED
#14 [runtime 5/5] COPY app/ ./app/
#14 DONE 0.0s
```

**Run, probe, and check users:**

```
$ docker run -d --name urdnot-verify -p 8000:8000 --stop-timeout 30 -e CLAN_SECRET="$(openssl rand -hex 16)" urdnot-api:0.1.0
$ curl -fsS localhost:8000/healthz
{"status":"ok"}
$ docker exec urdnot-verify id
uid=10001(urdnot) gid=10001(urdnot) groups=10001(urdnot)
$ docker top urdnot-verify -o pid,user,uid,cmd
PID                 USER                UID                 CMD
969                 10001               10001               python -m app
$ docker exec urdnot-verify touch /opt/urdnot/app/x
touch: cannot touch '/opt/urdnot/app/x': Permission denied
$ docker exec urdnot-verify sh -c 'tr "\0" " " </proc/1/cmdline'
python -m app
```

**Health, as Docker reports it:**

```
$ docker inspect -f '{{.State.Health.Status}} {{json (index .State.Health.Log 0)}}' urdnot-verify
healthy {"Start":"2026-09-25T12:57:41.41203959Z","End":"2026-09-25T12:57:41.726076049Z","ExitCode":0,"Output":""}
$ docker ps --filter name=urdnot-verify --format '{{.Names}}\t{{.Status}}'
urdnot-verify	Up 4 seconds (healthy)
```

**Graceful stop with a request in flight.** A 3 s `/api/charge` request was started, and `docker stop` was sent 0.5 s later:

```
$ time docker stop urdnot-verify
real 2.95
{"requested_ms":3000,"duration_ms":3005.575,"cpu_ms":3000.055}     # the in-flight request completed, curl exit 0
container exit=0
{"event": "Waiting for application shutdown."} … {"event": "Finished server process [1]"}
```

**Labels:**

```
$ docker inspect -f '{{json .Config.Labels}}' urdnot-api:0.1.0
org.opencontainers.image.description  Krogan-themed FastAPI service for a 12-week DevOps curriculum
org.opencontainers.image.licenses     MIT
org.opencontainers.image.revision     a20281b
org.opencontainers.image.source       https://github.com/Torehan06/urdnot
org.opencontainers.image.title        urdnot-api
org.opencontainers.image.version      0.1.0
```

**Tests after the requirements split,** in a host venv (`app/.venv`, Homebrew Python 3.12.14, the same patch release as the image) with `requirements-dev.txt`: `17 passed, 2 warnings in 3.80s`. Both warnings are upstream deprecation notices from starlette's `TestClient` and anyio, not from our code. The macOS system Python (3.9.6) cannot install the pins. For example, `annotated-types==0.8.0` has no 3.9 release.

**Vulnerability scan.** `docker scout` is installed with Docker Desktop, but it requires a Docker ID login, so the scan used Trivy 0.74.0 instead. Trivy ran as a throwaway container (`aquasec/trivy`), so nothing was installed on the host. The DB is dated 2026-09-25.

```
$ docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy image --scanners vuln urdnot-api:0.1.0
Target                                 CRITICAL  HIGH  MEDIUM  LOW  UNKNOWN
urdnot-api:0.1.0 (debian 13.7)                0    44      53   57        2
Python (site-packages)                        0     0       5    1        0
```

| Severity | Source | Findings | Fix available |
| --- | --- | --- | --- |
| Critical | — | none | — |
| High | **Base image** (Debian 13.7 OS packages) | 44 package entries, 8 unique CVEs: util-linux ×4 (CVE-2026-76642, -78408, -78409, -78410, repeated across `mount`, `libmount1`, `libblkid1`, `login`, and others), ncurses (CVE-2025-69720), acl (CVE-2026-54369), systemd libs (CVE-2026-16742), perl-base (CVE-2026-9538) | **None.** Debian marks 43 as "affected" and 1 as "fix_deferred". |
| Medium/Low (Python) | **Base image** (its system `pip 25.0.1` in `/usr/local/lib/python3.12`) | 6 pip CVEs | Yes, in pip 25.3 through 26.2 |
| Any | **Our dependencies** (the 17 pins in `/opt/venv`) | **none** | — |

How to read this:

- **Our code and our pinned dependencies are clean.** Every finding comes from `python:3.12.14-slim-trixie`.
- **The HIGH findings are mostly not reachable in this container.** They are in mount, nsenter, and login tooling, `systemd-homed`, ncurses, and Perl's `Archive::Tar`. The app runs as UID 10001 and never calls any of them. Many are local privilege-escalation bugs, and a non-root user with no setuid path reduces their impact. That is a judgment call, not a proof, and it should be revisited when fixes land.
- **Base-image findings are fixed by rebuilding on a newer base digest, not by patching in the Dockerfile.** Nothing can be patched today, because Debian has no fixes yet. This is the argument for automated base bumps (Dependabot or Renovate) and scheduled rebuilds.
- **The sandbox guessed "mostly low-severity" findings, and that was wrong.** A realistic slim Debian base carries dozens of HIGHs with no fix. That is normal, and it is why teams gate on "fixable critical/high" rather than raw counts.
- **The pip findings are avoidable.** They come from the base image's pip, which the runtime never uses. Deleting it in the runtime stage would remove them. This was not done yet (see the `--without-pip` bullet).

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
