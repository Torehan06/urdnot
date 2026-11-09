# Week 4: CI/CD with GitHub Actions

Every push and pull request now proves the service works before it can be merged. Every merge to `main` publishes a multi-architecture image to GitHub Container Registry (GHCR).

## What was built

- [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) has three jobs:
  - **`test`** runs the 17 unit tests on the runner's Python 3.12. It needs no Docker and gives feedback in about a minute.
  - **`smoke`** runs after `test`. It generates a throwaway `.env`, validates `compose.yaml`, builds the image, runs `docker compose up --wait`, and runs the smoke test against the live stack. If anything fails, it prints `ps` and the logs. It always runs `down -v`.
  - **`publish`** runs only on a push to `main` and only after `test` and `smoke` pass. It builds `linux/amd64` and `linux/arm64` and pushes `ghcr.io/torehan06/urdnot-api:sha-<7>` and `:main`.
- [`scripts/smoke-test.sh`](../scripts/smoke-test.sh) is an end-to-end check that runs the same way locally and in CI. It checks:
  - `/healthz` and `/readyz` both return 200.
  - The home page reports `Storage: postgres` and `Counter: redis`.
  - A POST without the secret gets 403, and a POST with it gets 201.
  - The new record can be read back.
  - The Redis counter goes up by exactly 1.
  - `/metrics` exposes `http_requests_total`.
- [`.github/dependabot.yml`](../.github/dependabot.yml) opens weekly grouped PRs for:
  - the SHA-pinned actions
  - the `==` pins in `app/requirements*.txt`
  - the Postgres and Redis images in `compose.yaml`

## Why each choice was made

- **Two test layers: unit tests first, then the full stack.** Unit tests are fast but use the in-memory and in-process fallbacks. They can't catch a broken `Dockerfile`, a wrong `DATABASE_URL` in `compose.yaml`, or an app that silently falls back to in-memory storage. The smoke test catches all three. The home-page check (`Storage: postgres`) exists because `/readyz` alone can't tell you which backends the app *chose*. `smoke` uses `needs: test`, so a broken unit test fails fast without spending minutes on an image build.
- **The smoke test is a script, not inline workflow YAML.** When CI goes red, you run `scripts/smoke-test.sh` on your laptop and get the same failure. Logic that only runs inside GitHub can only be debugged by pushing commits. That's the main thing rejected about writing the checks straight into `run:` steps.
- **The `.env` is generated per run, with no GitHub Secrets.** The Postgres password and clan secret only need to agree *within one run*, and everything is deleted by `down -v`. A stored secret would add something to rotate and protect for no benefit. `::add-mask::` replaces the values with `***` if any log line prints them.
- **Teardown uses `if: always()` and logs use `if: failure()`.** By default a step runs only if every earlier step succeeded. Without `always()`, a failing smoke test would skip cleanup. GitHub-hosted runners are discarded anyway, but a self-hosted runner would keep the containers. You only need the logs when something broke.
- **Actions are pinned to commit SHAs.** This follows the same reasoning as the image digests in Week 2. A tag like `v7` is a movable pointer, and a compromised action repository can repoint it at malicious code that then runs with your token. A SHA can't be repointed. The `# v7.0.1` comment keeps it readable, and Dependabot bumps both. The SHAs were resolved with `gh api` and `git ls-remote` on 2026-11-09.
- **Permissions follow least privilege.** The workflow default is `contents: read`. Only `publish` gets `packages: write`, and only in its own job. `persist-credentials: false` keeps the job token out of `.git/config`, because no step pushes with git. On pull requests from forks, GitHub gives the token read-only access anyway, which is another reason publishing happens only on `main`.
- **`concurrency` cancels superseded runs on branches, not on `main`.** If you push twice to a PR, only the newest run matters. On `main`, every merge should produce its own image, so those runs are never cancelled.
- **GHCR with the built-in `GITHUB_TOKEN`.** No registry account or long-lived token is needed. The image name is lowercased (`${GITHUB_REPOSITORY_OWNER,,}`) because registry names must be lowercase and the owner is `Torehan06`.
- **Two tags: `sha-<7>` and `main`.** `sha-…` is immutable and traceable to a commit, so Kubernetes manifests in later weeks should use it. `main` is a moving convenience tag. There's no `latest`, because nothing here defines a release yet.
- **Images for amd64 and arm64.** CI runners are amd64, and this Mac and many small clusters are arm64. QEMU emulates arm64 on the amd64 runner. That's affordable because the Dockerfile installs wheels only (`--only-binary=:all:`), so nothing is compiled under emulation. The `gha` build cache makes later builds reuse unchanged layers.
- **`publish` rebuilds the image instead of reusing the one `smoke` tested.** This is the main tradeoff of the week. The alternative is to push an image from every PR and smoke-test the pushed image. That needs `packages: write` on PR runs (unavailable for forks) and fills the registry with untested images. Builds here are reproducible (pinned base digest, `==` pins, the same `Dockerfile`), so both builds use the same inputs. They are not the same artifact, though.

## Verification

All of this was run locally on 2026-11-09 (Apple silicon, Docker 29.4.1, Compose v5.1.3). **The workflow itself has not run on GitHub yet.** That happens on the first push of this branch.

```text
$ python -m pytest app/tests -q
17 passed, 2 warnings

$ docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest -no-color -verbose
Found total 0 errors ... for .github/workflows/ci.yml

$ docker run --rm -v "$PWD:/mnt" koalaman/shellcheck:stable scripts/smoke-test.sh
(no output, exit 0)
```

The `smoke` job's steps were replayed under a separate Compose project (`COMPOSE_PROJECT_NAME=urdnot-ci`), using a generated env file, so the Week 3 `urdnot` volumes were not touched:

```text
Container urdnot-ci-api-1 Healthy
ok   /healthz 200
ok   /readyz 200
ok   storage=postgres, counter=redis
ok   POST without secret 403
ok   POST with secret 201
ok   record smoke-1794211420-13439 read back
ok   headbutts 1 -> 2
ok   /metrics exposes http_requests_total
smoke test passed against http://127.0.0.1:8000
```

The publish build was checked with `docker buildx build --platform linux/amd64,linux/arm64 .` (no push), which exited 0. The GHCR login and push were not tested.

## How to run it

- **In CI:** open a PR. The `test` and `smoke` jobs appear as checks. Merging to `main` runs `publish`. You can also run the workflow by hand from the Actions tab (`workflow_dispatch`); it won't publish unless it runs on `main` from a push.
- **Locally, the same smoke test:**

  ```sh
  docker compose up -d --build --wait
  set -a; . ./.env; set +a      # exports CLAN_SECRET
  scripts/smoke-test.sh
  ```

- **Pull a published image:** `docker pull ghcr.io/torehan06/urdnot-api:sha-<7>`.

## Break it once (diagnose from the run log before reading code)

Push a commit that changes `CLAN_SECRET` in `compose.yaml` to a fixed string, for example `CLAN_SECRET: nope`. Keep the `.env` generation as it is. The image still builds and every health check stays green. Find which job fails, which check fails, and what the response body says. Explain why `/readyz` didn't catch it. Then revert the change.

Locally, two failures were tried, and both failed with a readable reason:

```text
CLAN_SECRET=wrong  -> FAIL POST with secret returned 403: {"detail":"Invalid clan secret"}
redis stopped      -> FAIL /readyz returned 503: {"status":"not_ready","failing_dependencies":["redis"]}
```

## What you should be able to explain in an interview

- The difference between CI (proving each change works) and CD (shipping it automatically), and where the line is in this workflow.
- How `on: push` and `on: pull_request` differ: which commit is tested (for a PR it's a merge commit), and what token permissions a fork PR gets.
- Why `needs:` order matters, and what `if: always()`, `if: failure()`, and the default `success()` do.
- Why to pin actions by SHA, and what a supply-chain attack on an action could do with `packages: write`.
- How `GITHUB_TOKEN` is scoped per job with `permissions:`.
- What a multi-platform image is (one manifest list pointing to per-architecture images) and how QEMU builds a foreign architecture.
- Why deployments should reference `sha-…` tags or digests, never `main` or `latest`.

## Test your understanding

1. If `cancel-in-progress` were `true` everywhere, what could go missing when two PRs are merged a minute apart?
2. The smoke job runs `docker compose up --wait`. What exactly is it waiting for on the `api` service, and why is that `/healthz` and not `/readyz`?
3. A Dependabot PR bumps `postgres:18.6` to `18.7`. Which job would catch a breaking change, and which wouldn't?
4. Why does `publish` need `needs: [test, smoke]` and not just `needs: smoke`?
5. Someone opens a PR from a fork that edits `ci.yml` to print `secrets.GITHUB_TOKEN`. What can they actually get?

## Notes for Week 5 (not built yet)

- Kubernetes manifests should pull `ghcr.io/torehan06/urdnot-api:sha-<7>`. Check the package's visibility in GitHub (your profile → Packages → `urdnot-api` → Package settings) after the first publish. A private package needs an `imagePullSecret`.
- The smoke script takes `BASE_URL`, so it can be pointed at a port-forwarded Kubernetes Service as a post-deploy check.

## Assumptions and open items

- **No `test` stage in the `Dockerfile`.** It was listed in Week 2 and Week 3's notes. The `test` job already runs the suite against the same `==` pins, and a test stage would duplicate it at the cost of an extra build. Revisit if runner Python and image Python ever need to match exactly (3.12.x on the runner vs the image's pinned 3.12.14).
- **The Python base image in the `Dockerfile` isn't in Dependabot.** The `FROM` line uses `${PYTHON_IMAGE}` from an `ARG`, and Dependabot's Docker updater isn't known to follow `ARG` defaults (not verified). For now, bump the digest by hand, or switch to Renovate, which handles this.
- **Dependabot's `docker-compose` ecosystem is untested here.** It only shows up after the config reaches `main`. Check Insights → Dependency graph → Dependabot for errors.
- **The publish job is unverified until the first merge.** Possible first-run problems: the GHCR package's visibility, and the "Workflow permissions" setting under Settings → Actions → General. The per-job `permissions:` block should be enough, but an org or repo policy can override it.
- **No linting job (ruff, actionlint) in CI.** No new tools were added this week. actionlint and shellcheck were run locally through their official container images.
