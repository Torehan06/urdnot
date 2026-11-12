# Week 8: deployment runbook

Deploying an image with Helm, measuring whether the rollout loses requests, and rolling back. The pipeline runs these same commands (`.github/workflows/ci.yml`, jobs `k8s` and `deploy`). Design notes are in [docs/week-08.md](../../docs/week-08.md).

| Path | Contents |
| --- | --- |
| `../../scripts/deploy.sh` | `helm upgrade --install` for one environment and one image; waits, and rolls back on failure |
| `../../scripts/rollout-probe.sh` | Starts and stops the prober and counts failed requests |
| `prober.yaml` | The prober Pod: `curl` against `http://urdnot-api/readyz` five times a second |

The chart is still [`infra/week7/urdnot`](../week7/urdnot). Requirements are the same as in the [Week 7 runbook](../week7/README.md), including the git-ignored `infra/week7/values-local.yaml` with the secrets.

## Deploy an image

```sh
kubectl config current-context                  # must be the cluster you mean

# An image CI published (tag from the Actions run or the GHCR package page):
scripts/deploy.sh staging sha-66e9028

# With the digest, as the pipeline does:
scripts/deploy.sh staging sha-66e9028 sha256:62f28aac532a0c3df3e9bae2ab4193f090f133ad9ed4e19c44f65eb31b12202a

# A local build (Rancher Desktop shares its image store with k3s):
docker --context rancher-desktop build -t urdnot-api:week8-a --build-arg REVISION=week8-a .
IMAGE_REPOSITORY=urdnot-api scripts/deploy.sh staging week8-a
```

The script prints the last five revisions when it finishes. To see which build is serving:

```sh
curl -s http://staging.urdnot.localhost/ | grep -o 'Revision: [^<]*'
```

`values-local.yaml` may set `api.image` (Week 7's local-image block). `deploy.sh` overrides tag and digest, and the repository too when `IMAGE_REPOSITORY` is set.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NAMESPACE` | `urdnot-<environment>` | Target namespace |
| `IMAGE_REPOSITORY` | from the values files | e.g. `urdnot-api` for a local build |
| `VALUES_EXTRA` | `infra/week7/values-local.yaml` | Extra values files, space-separated, applied last |
| `TIMEOUT` | `3m` | How long to wait for the new Pods to be Ready |
| `KUBE_CONTEXT` | current context | Also read by `rollout-probe.sh` |

## Measure a rollout

```sh
scripts/rollout-probe.sh start urdnot-staging
IMAGE_REPOSITORY=urdnot-api scripts/deploy.sh staging week8-b
scripts/rollout-probe.sh stop urdnot-staging
# rollout probe in urdnot-staging: 49 requests, 0 failed
```

`check` prints the count and keeps the prober running. `stop` and `check` exit 1 if any request failed. Watch the Pods being replaced in a second terminal:

```sh
kubectl -n urdnot-staging get pods -l app.kubernetes.io/name=urdnot-api -w
```

To include the Ingress hop, send requests from the host as well:

```sh
while true; do printf '%s ' "$(date +%T)"; curl -s -o /dev/null -m 2 -w '%{http_code}\n' http://staging.urdnot.localhost/readyz; sleep 0.2; done
```

## Roll back

A deployment that doesn't become Ready within `TIMEOUT` is rolled back by Helm, and `deploy.sh` exits non-zero. Try it:

```sh
TIMEOUT=40s IMAGE_REPOSITORY=urdnot-api scripts/deploy.sh staging does-not-exist
```

The running Pods are not touched while this happens. To go back after a deployment that succeeded:

```sh
helm -n urdnot-staging history urdnot
helm -n urdnot-staging rollback urdnot <revision> --wait
```

Then revert the commit in Git. Otherwise the next merge deploys the same change again. Don't use `kubectl rollout undo` on a Helm release: Helm's history would no longer match the cluster.

## If a release is stuck

An interrupted `helm upgrade` can leave the release in `pending-upgrade`, and the next upgrade then stops with "another operation is in progress":

```sh
helm -n urdnot-staging status urdnot
helm -n urdnot-staging rollback urdnot <last deployed revision>
```

## In the pipeline

- Pull requests: the `k8s` job installs the pinned image on kind, upgrades to the commit's image under the prober, and then deploys a missing image to check the rollback.
- `main`: after `publish`, the `deploy` job does the same upgrade with the published `sha-<commit>` image and its digest. Each run appears under **Environments → test** on the repository page, and the job summary shows the Helm history.

The kind cluster is deleted with the job. Nothing in CI deploys to your local cluster.
