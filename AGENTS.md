# Agent guide for urdnot

`urdnot-api` is a small Python 3.12 FastAPI service that anchors a 12-week DevOps
curriculum (bare Linux -> Docker -> Compose -> CI/CD -> Kubernetes -> observability).
The owner is learning DevOps through it and reviews and commits everything you write.

> `CLAUDE.md` contains only `@AGENTS.md`, so this is the single source of truth.
> Edit `AGENTS.md`; never copy its content into `CLAUDE.md`.

## How to work here
- You may do the core work (code, configs, docs). You do not have to hand-hold.
- **Always explain what you did, so the owner understands what they are committing.**
  After a change, say in plain language:
  1. what changed and which files,
  2. why this approach (and the main alternative you rejected),
  3. how you verified it (command + result),
  4. anything uncertain, skipped, or assumed.
  Keep it short and concrete. Explain for someone who must be able to defend the commit,
  not a log of your own reasoning. Don't paste large diffs back; point to files.
- Don't present unverified things as done. If a test or command failed, say so with the output.
- Ask before anything outward-facing or hard to reverse (push, PR, deleting files,
  force operations, anything touching real cloud resources).

## Layout
- `app/` - the service (`app/tests/`, `requirements.txt`, `requirements-dev.txt`)
- `Dockerfile`, `.dockerignore` - the container image (Week 2)
- `compose.yaml`, `.env.example` - API + Postgres + Redis stack (Week 3); real values go in the git-ignored `.env`
- `.github/workflows/ci.yml`, `.github/dependabot.yml` - CI/CD (Week 4): tests, Compose smoke test, GHCR publish on `main`
- `scripts/smoke-test.sh` - end-to-end check against a running stack, used locally and in CI
- `infra/weekN/` - server and deployment config, one folder per curriculum week
- `docs/week-NN.md` - the write-up for each week
- `private/` - personal files; never read, quote, or commit from here

## Commands
Run from the repo root with the venv active (`python3.12 -m venv app/.venv`).
- Run locally: `python -m app`
- Tests: `python -m pytest app/tests -q` (need no Postgres or Redis)
- Build image: `docker build -t urdnot-api:0.1.0 .`
- Full stack: `docker compose up -d --build --wait` (needs `.env`; see `.env.example`)
- Smoke test: `set -a; . ./.env; set +a; scripts/smoke-test.sh` (stack must be up)

## Conventions
- One branch per curriculum week (`week-02-docker`); merge via PR into `main`.
- Conventional-style commits (`feat:`, `docs:`); short imperative summary.
- Configuration comes only from environment variables (table in `README.md`).
  Update that table when you add or change one.
- Never bake secrets into the Dockerfile (`ENV`/`ARG` are readable via `docker history`).
  Use runtime env vars, a git-ignored `.env`, or Compose secrets. The default
  `CLAN_SECRET` is for local development only.
- Liveness vs readiness: container health checks use `/healthz`, never `/readyz`.
- Each week gets a doc in `docs/` covering design, verification, and "test your
  understanding" questions. When you finish a week's work, update or add that doc.
- Don't add dependencies, frameworks (e.g. a migration tool), or later-week
  infrastructure unless the current week calls for it.
