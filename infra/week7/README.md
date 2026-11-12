# Week 7: Helm chart runbook

The Week 5 and 6 manifests as one Helm chart, installed once per environment. Design notes are in [docs/week-07.md](../../docs/week-07.md).

| Path | Contents |
| --- | --- |
| `urdnot/Chart.yaml` | Chart name, chart version, app version |
| `urdnot/values.yaml` | Default values: every setting the templates read |
| `urdnot/templates/` | The API (Service, Deployment, PodDisruptionBudget, and since Week 9 an optional HorizontalPodAutoscaler), Ingress, ConfigMap, Secret, Postgres, Redis, and `_helpers.tpl` |
| `values-dev.yaml` | dev: 1 replica, `debug` logging, host `dev.urdnot.localhost` |
| `values-staging.yaml` | staging: 2 to 5 replicas set by the autoscaler (Week 9), `info` logging, host `staging.urdnot.localhost` |
| `values-local.yaml.example` | Template for the git-ignored `values-local.yaml` (secrets, optional local image) |

## Requirements

- Helm 3.8 or newer (verified with Helm 4.2.1) and a cluster with an Ingress controller. k3s (Rancher Desktop) ships Traefik.
- About 4 GB of memory in the Rancher Desktop VM to run both environments.
- An image with `CLAN_SECRET_FILE` support. The default pin in `urdnot/values.yaml` (`sha-66e9028`, bumped in Week 8) has it.

## Secrets

```sh
cp infra/week7/values-local.yaml.example infra/week7/values-local.yaml
# replace both values, e.g. with: openssl rand -hex 24
```

`values-local.yaml` is git-ignored. Without it the install stops with `secrets.postgresPassword is required`.

## Install an environment

One command per environment, from the repository root:

```sh
kubectl config current-context                  # must be the local cluster

helm upgrade --install urdnot infra/week7/urdnot -n urdnot-dev --create-namespace \
  -f infra/week7/values-dev.yaml -f infra/week7/values-local.yaml --wait

helm upgrade --install urdnot infra/week7/urdnot -n urdnot-staging --create-namespace \
  -f infra/week7/values-staging.yaml -f infra/week7/values-local.yaml --wait
```

`upgrade --install` installs the release if it doesn't exist and upgrades it if it does, so the same command works the first time and every time after. Later `-f` files override earlier ones.

```sh
helm list -A
kubectl -n urdnot-dev get pods,svc,ingress,pvc
curl -i http://dev.urdnot.localhost/healthz
set -a; . infra/week5/secrets.env; set +a       # or export CLAN_SECRET yourself
BASE_URL=http://dev.urdnot.localhost scripts/smoke-test.sh
BASE_URL=http://staging.urdnot.localhost scripts/smoke-test.sh
```

`--create-namespace` creates a plain namespace. To get the Pod Security labels that `infra/week5/namespace.yaml` set:

```sh
kubectl label namespace urdnot-dev pod-security.kubernetes.io/enforce=baseline pod-security.kubernetes.io/warn=restricted
```

## Use a locally built image

Rancher Desktop's Docker and k3s share an image store, so no registry is needed:

```sh
docker --context rancher-desktop build -t urdnot-api:week7-local .
```

Then uncomment the `api.image` block in `values-local.yaml`.

## Look before you install

```sh
helm lint infra/week7/urdnot -f infra/week7/values-dev.yaml -f infra/week7/values-local.yaml
helm template urdnot infra/week7/urdnot -f infra/week7/values-dev.yaml -f infra/week7/values-local.yaml | less
helm -n urdnot-dev get manifest urdnot          # what the release applied
helm -n urdnot-dev get values urdnot            # the values you supplied (includes the secrets)
```

## Change a setting, and roll back

```sh
# edit values-dev.yaml (or add --set api.config.logLevel=warning), then run the install command again
helm -n urdnot-dev history urdnot
helm -n urdnot-dev rollback urdnot 1            # back to revision 1, recorded as a new revision
```

A changed ConfigMap value rolls the API Pods. A changed `secrets.clanSecret` doesn't: the kubelet rewrites the mounted file within about a minute and the app re-reads it (Week 6). Changing `secrets.postgresPassword` on an existing database breaks the API; see the Week 5 notes.

## Package and publish the chart

```sh
helm package infra/week7/urdnot                 # writes urdnot-<version>.tgz (0.2.0 now)
helm registry login ghcr.io -u <github-user>    # token needs the write:packages scope
helm push urdnot-0.2.0.tgz oci://ghcr.io/torehan06/charts
helm install urdnot oci://ghcr.io/torehan06/charts/urdnot --version 0.2.0 -n urdnot-dev --create-namespace -f ...
```

A version can be pushed only once in practice: bump `version` in `Chart.yaml` for every change to the chart. Don't commit the `.tgz`.

## Remove

```sh
helm -n urdnot-dev uninstall urdnot
kubectl -n urdnot-dev get pvc                   # data-postgres-0 and data-redis-0 are still there
```

`helm uninstall` deletes everything the chart created, but not the PersistentVolumeClaims: a StatefulSet creates those itself from `volumeClaimTemplates`, so Helm never owned them. Installing again reattaches the same data. To delete the data too, delete the namespace (`kubectl delete namespace urdnot-dev`). `local-path` has `reclaimPolicy: Delete`, so the volumes go with it.

The Week 6 stack in the `urdnot` namespace is separate and can run next to these. Remove it with `kubectl delete -k infra/week6` (this deletes its data).
