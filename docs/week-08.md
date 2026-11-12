# Week 8: CI/CD into Kubernetes

Week 8 connects the pipeline from Week 4 to the Helm chart from Week 7. After a merge to `main`, GitHub Actions publishes the image and then runs `helm upgrade` with exactly that image on a Kubernetes cluster, with no manual step. The deployment is measured: a prober sends requests during the rollout and the pipeline fails if a single one is lost. A broken image is rolled back automatically. The runbook is [infra/week8/README.md](../infra/week8/README.md).

## What was built

| Change | Where | Notes |
| --- | --- | --- |
| Deploy script | `scripts/deploy.sh` | `scripts/deploy.sh <dev\|staging> <image-tag> [digest]`. One `helm upgrade --install` that waits for the rollout and rolls back if it fails. Used by CI and by hand. |
| Rollout prober | `infra/week8/prober.yaml`, `scripts/rollout-probe.sh` | A Pod that requests `/readyz` through the Service five times a second. `start`, `check`, `stop`; non-zero exit if any request failed. |
| Upgrade rehearsal on every PR | `.github/workflows/ci.yml`, job `k8s` | Installs the previous release, upgrades to the commit's image under the prober, runs the smoke test, then checks that a missing image is rolled back. |
| Continuous deployment | `.github/workflows/ci.yml`, job `deploy` | On `main`, after `publish`: deploys the image that was just pushed (by tag and digest) to a test cluster, again under the prober. |
| Visible revision | `Dockerfile`, `app/main.py` | The image carries `APP_REVISION` (the commit). The home page shows it, so you can see which build answered. |
| Image pins | `infra/week7/urdnot/values.yaml`, `infra/week5/kustomization.yaml` | Bumped to `sha-66e9028`, the first pin that understands `CLAN_SECRET_FILE`. Chart version 0.1.1. |

## The pipeline

```text
pull request:  test ──► smoke
                   └──► k8s      previous release ─► upgrade to this commit ─► 0 lost requests
                                 ─► smoke test ─► broken image is rolled back

merge to main: the same three, then
               publish ──► deploy
               push sha-<commit>   helm upgrade to that tag@digest ─► 0 lost requests ─► smoke test
```

Two ideas hold it together:

- **The image is an argument, not a file edit.** Until now the image lived in `values.yaml`, and deploying a new build meant editing that pin by hand. `deploy.sh` passes `--set api.image.tag` and `api.image.digest`. The `publish` job hands its tag and digest to the `deploy` job as job outputs, so what gets deployed is exactly what was pushed. The pin in `values.yaml` is now only a default for manual installs.
- **The pull request rehearses what the merge will do.** The `k8s` job runs the same script with the same flags as the `deploy` job. A change that breaks deployment (a template error, a Pod that never gets Ready) fails the PR, before `main` is touched.

## How a rolling update avoids downtime

`helm upgrade` changes the image in the Deployment's Pod template. The Deployment controller then replaces Pods step by step. Each setting below was already in the chart; this week is where they are put to the test.

| Setting (`templates/api.yaml`) | What it does during a rollout |
| --- | --- |
| `maxUnavailable: 0`, `maxSurge: 1` | Never go below the wanted number of Ready Pods. Start one extra new Pod, and remove an old one only after the new one is Ready. |
| `readinessProbe` on `/readyz` | A Pod receives traffic only while Ready. A new Pod that can't reach Postgres or Redis never gets traffic, and the rollout stops there. |
| `startupProbe` on `/healthz` | Gives the process time to start before the other probes count. |
| `preStop: sleep 5` | Removing a Pod from the Service's endpoints and stopping it happen in parallel. The sleep keeps the old Pod serving while every proxy learns it is gone. Without it, requests can be sent to a Pod that has already exited. |
| SIGTERM handling (`app/__main__.py`) | After the sleep, the kubelet sends SIGTERM. Uvicorn stops accepting connections and finishes the requests in flight. |
| `terminationGracePeriodSeconds: 30` | The upper limit for sleep plus drain. After that the kubelet sends SIGKILL. |

With two replicas the sequence is: third Pod (new) starts → becomes Ready → one old Pod leaves the endpoints, sleeps 5 s, drains, exits → a second new Pod starts → Ready → the last old Pod goes. At every moment at least two Pods are Ready.

`helm upgrade --wait` returns only when the Deployment reports all replicas updated and Ready. Without `--wait`, Helm returns as soon as the API server accepts the manifests, and the pipeline would show green before a single new Pod exists.

## Rollback strategies

| Strategy | Command | When |
| --- | --- | --- |
| Automatic, during the deploy | `--rollback-on-failure` (Helm 4; `--atomic` in Helm 3) | The new Pods don't get Ready within `--timeout`. Helm re-applies the previous revision and the command fails. `deploy.sh` always sets it. |
| Manual, after the deploy | `helm -n <ns> rollback urdnot <revision>` | The rollout succeeded, but the new version misbehaves in a way no probe catches. |
| Roll forward | `git revert <commit>`, merge | The normal answer in a pipeline. The revert goes through tests and the same deployment, and `main` matches what is running. |
| Not with Helm | `kubectl rollout undo` | It changes the Deployment behind Helm's back: `helm history` then no longer describes the cluster. |

Three things worth knowing:

- **During a failed rollout the old version keeps serving.** Because of `maxUnavailable: 0`, no old Pod is removed until a new one is Ready. A bad image costs a failed pipeline, not an outage. The rollback only cleans up the stuck new Pod.
- **A rollback restores manifests, not data.** If the new version changed the database, rolling back the image doesn't undo it. That is why schema changes must stay compatible with the previous version.
- **A manual rollback and a pipeline disagree.** After `helm rollback`, the next merge deploys the newest image again, including the bad change, unless it was reverted in Git. Roll back to stop the bleeding, then revert.

On a first install there is no previous revision: a failed install with this flag is uninstalled instead.

## Why each choice was made

- **A kind cluster inside the job as the deployment target.** GitHub's runners can't reach the cluster on a laptop, and there is no cloud cluster until later weeks. kind gives the pipeline a real Kubernetes API with nothing to pay for and no credentials to store. The cost: the cluster disappears with the job, so no environment stays up. Pointing the `deploy` job at a real cluster means swapping the kind step for that cluster's credentials; `deploy.sh` stays the same. The rejected alternative was a self-hosted runner on the laptop: on a public repository, a pull request from anyone could run code on it.
- **Upgrade from the previous release, not a fresh install.** A fresh install can't lose requests, because nothing was serving. Each job first installs the pinned image, then upgrades. That is the only way to test the thing the week is about.
- **A prober inside the cluster, not a port-forward.** `kubectl port-forward` tunnels to one Pod and breaks when that Pod is replaced, so it would report downtime that users never see. The prober makes a new connection per request through the Service, so each request goes to whichever Pods are Ready at that moment.
- **Scripts instead of workflow YAML.** `deploy.sh` and `rollout-probe.sh` run the same on a laptop as in CI, like `smoke-test.sh` since Week 4. A failed deployment can be reproduced without pushing commits to see what happens.
- **Tag and digest.** The tag (`sha-66e9028`) tells a person which commit runs. The digest guarantees the bytes are the ones `publish` pushed.
- **`concurrency: deploy-test` without cancelling.** Two merges in a row deploy one after the other. Cancelling a `helm upgrade` halfway can leave a release in `pending-upgrade`, which blocks the next one.
- **A GitHub Environment (`test`).** It gives a deployment history on the repository page. It is also where approval rules and environment-scoped secrets would go for a real cluster. None are configured.
- **No pull-based deployment yet.** This is the push model from Week 7's GitOps section: the pipeline runs `helm upgrade`, so the pipeline needs access to the cluster, and nobody notices manual changes to the cluster afterwards. With kind there are no credentials to protect. For a real cluster this is the main argument for Argo CD or Flux.

## Verification

Run on 2026-11-11 on Rancher Desktop (k3s, context `rancher-desktop`), Helm 4.2.1, in the `urdnot-staging` namespace (2 replicas). Two images were built from this branch, `urdnot-api:week8-a` and `week8-b`, differing only in `REVISION`.

**Unit tests:** `python -m pytest app/tests -q` → `22 passed` (one new test for the revision on the home page).

**Static checks:** `helm lint --strict` passes for dev and staging. `actionlint` reports nothing for `ci.yml`. `kubectl apply --dry-run=server` accepts `prober.yaml`.

**Zero-downtime upgrade.** Four upgrades between the two images (`scripts/deploy.sh staging week8-b`, and back), each taking 8 to 11 seconds. Requests were sent the whole time from two places:

| Probe | Requests | Failed |
| --- | --- | --- |
| Prober Pod through the Service, three consecutive upgrades | 377 | 0 |
| Prober Pod, first upgrade | 49 | 0 |
| `curl` from the Mac through the Ingress, three upgrades with timestamps | 330 | 0 |
| `curl` from the Mac through the Ingress, first upgrade | 150 | 1 (no HTTP answer) |

The home page showed `Revision: week8-a` before and `week8-b` after. The one failed request in the first Ingress run was not reproduced in the three runs that followed, and that run had no timestamps, so its cause is unknown (see open items).

**Failed deployment.** `TIMEOUT=40s scripts/deploy.sh staging does-not-exist`:

```text
Error: UPGRADE FAILED: release urdnot failed, and has been rolled back due to rollback-on-failure being set
7   failed     Upgrade "urdnot" failed: resource Deployment/urdnot-staging/urdnot-api not ready …
8   deployed   Rollback to 6
deploy exit=1
```

The two API Pods had the same names before and after, the Deployment's image was `week8-a` again, and the same prober that watched the three upgrades had counted 648 requests with 0 failed by the end of the failed deployment. The smoke test passed through the Ingress afterwards.

**Not verified:**
- The changed `k8s` job. It runs when this branch is pushed.
- The `deploy` job. It runs only on `main`, so its first run is the merge of this week's pull request. Its steps are the same scripts as the `k8s` job, plus pulling the image from GHCR by digest.
- `--atomic` on Helm 3. The script selects it when `--rollback-on-failure` is missing; only Helm 4 was available to test.
- A release that fails its readiness probe (as opposed to a missing image). Same mechanism, not run.

## What you should be able to explain in an interview

- The path of a change from `git push` to a running Pod, job by job, and what each job protects against.
- Why the pipeline passes the image to Helm instead of committing a new tag to `values.yaml`, and what the tag and the digest are each for.
- `maxSurge` and `maxUnavailable`: what `1` and `0` mean, and what happens with `0` and `1` instead.
- Why a readiness probe, a `preStop` sleep and SIGTERM handling are all needed for zero downtime, and what breaks without each one.
- What `helm upgrade --wait` waits for, and why a pipeline without it reports success too early.
- Automatic rollback, `helm rollback`, and revert-and-redeploy: when to use which, and what none of them restore.
- Why the old version keeps serving during a failed rollout.
- Push deployment vs GitOps: where the cluster credentials live in each.

## Test your understanding

1. Deploy a local image with `scripts/deploy.sh`, start the prober, and in another terminal run `kubectl -n urdnot-staging get pods -w` during the next deploy. Write down the order of events. Where exactly do `maxSurge: 1` and `maxUnavailable: 0` show up?
2. Remove the `preStop` block from `templates/api.yaml` and repeat with the prober. Do requests fail? Run it several times. Why might the result differ between runs? Put the block back.
3. Set `maxUnavailable: 1` and `maxSurge: 0`, and deploy an image tag that doesn't exist. How many Pods serve during the failed rollout, compared with the current settings?
4. Run `helm upgrade` by hand without `--wait` for a missing image. What does Helm report, and what does `kubectl get pods` show? Fix it with `helm rollback`.
5. After a failed deploy, read `helm -n urdnot-staging history urdnot`. Which revision is `deployed`? Why is its number not the one you were on before?
6. In `ci.yml`, why does `deploy` have `cancel-in-progress: false` while the workflow's own concurrency group cancels older runs on branches?
7. The `publish` job needs `packages: write`. Why does `deploy` not need it, and what would it need for a private image?
8. Someone runs `helm rollback` on a real cluster at 14:00. At 14:30 an unrelated change merges. What is running at 14:40, and what should have been done at 14:05?

## Assumptions and open items

- **No environment stays running.** The test cluster lives for one job. `urdnot-staging` on Rancher Desktop is deployed only by hand (`scripts/deploy.sh`). A persistent target needs a reachable cluster (Week 10 creates cloud infrastructure).
- **One lost request in the first Ingress measurement, cause unknown.** 1 of 480 requests through the Ingress got no answer; none of the 697 through the Service did. Candidates: Traefik still routing to a Pod that had just exited, or the port-forward between macOS and the Rancher Desktop VM. To investigate, repeat with timestamps and compare against Traefik's access log. If it is Traefik, a longer `preStop` sleep is the usual fix.
- **The prober measures the Service path only in CI.** kind has no Ingress controller, so the Ingress hop is not covered there.
- **The prober checks one request at a time.** It would not catch long requests cut off by a shutdown. A load test with concurrent requests (Week 9 uses k6 or ab) covers that.
- **The baseline for the upgrade is the pinned image**, not "whatever was deployed last". As the pin ages, the rehearsal upgrades across more commits. Bump it now and then; nothing depends on it for deployments.
- **The Postgres and Redis image pins are still in three places** and the Kustomize weeks are still only rendered by CI (as in Week 7).
- **The chart is still not published from CI.** Deployments use the chart from the checked-out commit, so chart and image versions always match.
- **Database migrations are not handled.** The app creates its table at startup; there is no migration step in the deployment and no tool for it yet.
