# Week 7: Helm and an introduction to GitOps

Week 7 packages the Kubernetes manifests from Weeks 5 and 6 as a Helm chart, [`infra/week7/urdnot`](../infra/week7/urdnot/). The same chart is installed twice on the local cluster, as a dev and a staging environment, each with one command and its own values file. Nothing in the app changed. The runbook is [infra/week7/README.md](../infra/week7/README.md).

## What was built

| Change | Where | Notes |
| --- | --- | --- |
| Chart `urdnot` 0.1.0 | `infra/week7/urdnot/` | API, Ingress, ConfigMap, Secret, Postgres and Redis. Renders the same objects as `kubectl kustomize infra/week6` (compared field by field, see Verification). |
| Default values | `infra/week7/urdnot/values.yaml` | Replicas, image, `CLAN_NAME`, `LOG_LEVEL`, resources, Ingress class and host, storage sizes, secrets |
| dev environment | `infra/week7/values-dev.yaml` | 1 replica, `debug`, `dev.urdnot.localhost`, namespace `urdnot-dev` |
| staging environment | `infra/week7/values-staging.yaml` | 2 replicas, `info`, `staging.urdnot.localhost`, namespace `urdnot-staging` |
| Secrets and local image | `infra/week7/values-local.yaml` (git-ignored) | Template in `values-local.yaml.example` |
| CI | `.github/workflows/ci.yml` | The `k8s` job lints and renders the chart for both environments, then installs it on kind with `helm upgrade --install` instead of `kubectl apply -k` |

## Chart, values, templates, release

- A **chart** is a directory: `Chart.yaml` (name and versions), `values.yaml` (defaults) and `templates/` (Kubernetes YAML with placeholders).
- **Templates** are Go templates. `{{ .Values.api.replicaCount }}` is replaced with a value before anything is sent to the cluster. Helm renders on the client, then applies the result. `helm template` shows that result without a cluster.
- **Values** are merged in order: the chart's `values.yaml`, then each `-f` file from left to right, then `--set`. An environment file holds only what differs.
- A **release** is one installation of a chart, with a name and a namespace. Helm stores each revision of a release (values and rendered manifests) in a Secret named `sh.helm.release.v1.<name>.v<N>` in that namespace. That record is what makes `helm history`, `helm rollback` and `helm uninstall` possible. `kubectl apply -k` keeps no such record.

## From Kustomize to Helm

The two tools solve the same problem differently. Kustomize patches finished YAML. Helm fills in a template. Three Kustomize features needed a replacement:

| Week 5/6 (Kustomize) | Week 7 (Helm) |
| --- | --- |
| `configMapGenerator` adds a hash to the name, so a change rolls the Pods | The ConfigMap has a fixed name. The Deployment's Pod template carries `checksum/config`, a SHA-256 of the rendered ConfigMap. A changed value changes the annotation, which changes the Pod template, which starts a rollout. |
| `secretGenerator` reads `secrets.env` | The Secret is a template filled from `secrets.*` values, supplied from a git-ignored file. `required` stops the install if one is empty. |
| `namespace:` and a Namespace manifest | `helm install -n <ns> --create-namespace`. The chart contains no Namespace, so one chart can go into any namespace. |
| `images:` swaps the image | `api.image.repository`, `tag` and `digest` values |
| An overlay directory per variation | A values file per environment |

## Why each choice was made

- **One chart with Postgres and Redis inside, not subcharts.** The public Postgres and Redis charts (Bitnami's, for example) would replace the pinned images and the manifests explained in Week 5 with hundreds of lines of someone else's templates. Keeping them as two templates here means every line is still one you can explain. A real team would more likely use a managed database or an operator, and the chart would deploy only the API.
- **Fixed resource names, not `<release>-urdnot-api`.** Generated charts prefix every name with the release name, so two releases can share a namespace. Here the names stay `urdnot-api`, `postgres` and `redis`: the manifests read like Week 5's, and `DATABASE_URL` can keep pointing at `postgres`. The cost: one release per namespace. Environments are separated by namespace, so this never collides.
- **dev and staging as two namespaces in one cluster.** That's enough to show one chart producing two different deployments. Real environments are usually separate clusters; the commands are the same with a different `--kube-context`.
- **Values files hold only differences.** `values-dev.yaml` is seven lines of settings. If a default changes in `values.yaml`, both environments get it, and a reviewer sees at a glance how dev differs from staging.
- **No PodDisruptionBudget with one replica.** `minAvailable: 1` on a single Pod forbids every voluntary eviction, so a node drain would hang. The template renders the PDB only when `api.replicaCount` is above 1. A patch can't express that kind of condition; a template can.
- **Secrets through a git-ignored values file.** It keeps one way of passing settings. The trade-off is that Helm stores supplied values in the release Secret, so `helm get values` shows them to anyone who can read Secrets in the namespace. Such a person could read `urdnot-secrets` anyway, so nothing new leaks. `secrets.existingSecret` covers the stricter setup: someone else (or a tool such as External Secrets or Sealed Secrets) creates the Secret and the chart only references it.
- **The clan secret stays a mounted file with a fixed Secret name**, so Week 6's live rotation still works through `helm upgrade`.
- **`upgrade --install` everywhere.** One idempotent command for first install and every later change. It's also the command a pipeline runs (Week 8).
- **`version` and `appVersion` are separate.** `version` (0.1.0) is the chart's own and must change whenever the chart does. `appVersion` describes the app and is informational. What actually runs is `api.image`.
- **Weeks 5 and 6 stay in the repository** as the record of those weeks. CI no longer deploys them but still renders `infra/week6`, so a broken edit there fails the build.
- **The chart is published by hand this week, not from CI.** Publishing to an OCI registry is three commands (runbook). Doing it on every merge needs a versioning rule, which belongs with Week 8's deployment automation.

## Introduction to GitOps

Everything so far is **push** deployment: a person or a pipeline runs `kubectl apply` or `helm upgrade` against the cluster. That needs cluster credentials wherever the command runs, and nothing notices if someone later changes the cluster by hand.

**GitOps** turns it around. A controller inside the cluster watches a Git repository and keeps the cluster equal to what the repository says:

- **Git is the single source of truth.** A deployment is a merged commit. A rollback is `git revert`.
- **Pull, not push.** The cluster fetches its own configuration, so CI needs no cluster credentials.
- **Continuous reconciliation.** The controller compares desired state (Git) with actual state (cluster) all the time. A manual `kubectl edit` shows up as drift and can be reverted automatically.

The two common controllers:

- **Argo CD** has an `Application` object: a repository, a path or chart, a list of values files, and a destination cluster and namespace. This week's layout maps onto it directly: one `Application` for dev with `values-dev.yaml`, one for staging with `values-staging.yaml`. It has a web UI that shows sync status and drift.
- **Flux** is a set of smaller controllers. A `GitRepository` or `OCIRepository` object says where the source is, and a `HelmRelease` object says which chart and values to install. It has no built-in UI and is driven entirely by these objects.

Both render the chart and apply it; neither needs someone to run `helm upgrade`. Note that Argo CD renders Helm charts with `helm template` and applies the output itself, so `helm list` doesn't show its releases. Flux's helm-controller performs real Helm releases.

Secrets are the hard part of GitOps: they can't sit in Git as plain text. The usual answers are Sealed Secrets or SOPS (encrypted in Git) or External Secrets (fetched from a vault). The chart's `existingSecret` value is the hook for that.

Neither controller is installed this week. Week 8 automates deployment the push way (`helm upgrade` from GitHub Actions); knowing the pull model makes it clear what that approach trades away.

## Verification

Run on 2026-11-11, locally, on Rancher Desktop with k3s v1.36.2 (arm64) and Helm 4.2.1. Every command used the `rancher-desktop` context. The image was built from this branch (`urdnot-api:week7-local`), because the published pin predates `CLAN_SECRET_FILE`. The Week 6 stack in the `urdnot` namespace kept running next to both environments.

**Unit tests:** `python -m pytest app/tests -q` → `21 passed` (no app change this week).

**Lint and render.** `helm lint --strict` passes for both values files (one informational note: no chart icon). Without secrets, `helm template` stops with `secrets.postgresPassword is required`.

**Same objects as Week 6.** The chart's default render was compared with `kubectl kustomize infra/week6`, object by object and field by field, after both went through `kubectl create --dry-run=client`:

```text
Ingress, PodDisruptionBudget, 3 Services, 2 StatefulSets   identical
ConfigMap     name urdnot-config instead of urdnot-config-<hash>
Deployment    envFrom points at urdnot-config; new checksum/config annotation
Secret        same keys (written as stringData instead of data)
Namespace     only in Kustomize (Helm creates it with --create-namespace)
```

The chart also adds three labels to every object (`app.kubernetes.io/instance`, `app.kubernetes.io/managed-by`, `helm.sh/chart`).

**Two environments, one command each:**

```text
$ helm upgrade --install urdnot infra/week7/urdnot -n urdnot-dev --create-namespace \
    -f infra/week7/values-dev.yaml -f infra/week7/values-local.yaml --wait
STATUS: deployed   REVISION: 1   Replicas: 1   Log level: debug

$ helm upgrade --install urdnot infra/week7/urdnot -n urdnot-staging --create-namespace \
    -f infra/week7/values-staging.yaml -f infra/week7/values-local.yaml --wait
STATUS: deployed   REVISION: 1   Replicas: 2   Log level: info
```

| | dev | staging |
| --- | --- | --- |
| API Pods | 1 | 2 |
| `LOG_LEVEL` in the Pod | `debug` | `info` |
| Home page clan name | `Urdnot (dev)` | `Urdnot (staging)` |
| PodDisruptionBudget | none | `urdnot-api`, minAvailable 1 |
| Ingress host | `dev.urdnot.localhost` | `staging.urdnot.localhost` |
| `scripts/smoke-test.sh` through the Ingress | passed | passed |

Each environment has its own Postgres and Redis: the Redis counter started at 1 in both. With all three stacks running, the node used 2.3 GB of its 4 GB.

**Upgrade and rollback** (dev):

```text
$ helm upgrade … --set api.config.logLevel=warning     → new API Pod, LOG_LEVEL=warning
$ helm rollback urdnot 1                               → new API Pod, LOG_LEVEL=debug
$ helm history urdnot
1  superseded  Install complete
2  superseded  Upgrade complete
3  deployed    Rollback to 1
```

The config change rolled the Pod only because of the `checksum/config` annotation. Running the unchanged install command again changed nothing in the cluster and kept the same Pod.

**Live secret rotation through Helm** (dev). `helm upgrade … --set secrets.clanSecret=<new>`: the new secret was accepted after 9 s, the old one returned 403, and the same Pod kept serving with 0 restarts. Restoring the original took 79 s to arrive (the kubelet's sync timing, as in Week 6). The test wrote a few `helm-rotation` records to dev's Hollows table.

**Uninstall and reinstall** (dev). `helm uninstall` removed every object the chart created. The two PersistentVolumeClaims stayed. After installing again, the record written before the uninstall was still returned.

**Packaging.** `helm package infra/week7/urdnot` produced `urdnot-0.1.0.tgz` with `Chart.yaml`, `values.yaml` and the eight template files.

**Not verified:**
- CI with the new Helm steps. `actionlint` passes locally, but the job itself runs only when this branch is pushed. The Helm version on GitHub's runner is not known; the steps use only commands that exist in Helm 3 and 4.
- `helm push` to GHCR. Not run: it publishes a package, so it waits for the owner.
- The default image pin with this chart. It can't work until an image with `CLAN_SECRET_FILE` is published (see open items).
- `secrets.existingSecret` on a cluster. Only its render was checked (no Secret created, all three references renamed).
- Argo CD and Flux. Described, not installed.

## What you should be able to explain in an interview

- Chart, values, template, release, revision: what each is, and where Helm keeps release history.
- The order in which values are merged, and why environment files hold only differences.
- `helm template` vs `helm install --dry-run` vs `helm upgrade --install`, and why the last one is used in pipelines.
- Why a ConfigMap change doesn't restart Pods by itself, and how a checksum annotation fixes that.
- What `helm rollback` restores (the manifests of an old revision) and what it doesn't (data in a database, a PVC's contents).
- Why `helm uninstall` leaves StatefulSet PVCs behind.
- Kustomize vs Helm: patching vs templating, and when each is the better fit.
- Push vs pull deployment, what "reconciliation" and "drift" mean, and how Argo CD and Flux differ.

## Test your understanding

1. Run `helm template urdnot infra/week7/urdnot -f infra/week7/values-dev.yaml -f infra/week7/values-local.yaml` and the same for staging, then `diff` the two outputs. Can you explain every differing line from the two values files?
2. Remove the `checksum/config` annotation from `templates/api.yaml`, then change `logLevel` in `values-dev.yaml` and upgrade. What does `kubectl -n urdnot-dev exec deploy/urdnot-api -- printenv LOG_LEVEL` print, and why? Put the annotation back.
3. Swap the order of the `-f` flags so `values-local.yaml` comes first. What could go wrong if both files set the same key?
4. Change `postgres.storage` to `2Gi` and run the upgrade. Read the error. Why does Kubernetes refuse, and what does that tell you about what a Helm upgrade can and can't change?
5. Run `kubectl -n urdnot-dev get secret` and find Helm's release Secrets. How many are there after three upgrades, and what limits their number (`--history-max`)?
6. Set `api.replicaCount: 1` for staging and upgrade. What happens to the PodDisruptionBudget, and how did Helm know to delete it?
7. Edit the live Deployment by hand (`kubectl -n urdnot-dev scale deploy/urdnot-api --replicas=3`), then run the unchanged install command. Does Helm put it back? How would Argo CD or Flux have behaved differently, and when?
8. A teammate wants a `prod` environment. Which files would you add, and which values would you insist are different from staging?

## Assumptions and open items

- **Bump the image pin in `values.yaml` after Week 6 is merged.** Same issue as Week 6: `sha-0b1eec1` doesn't know `CLAN_SECRET_FILE`, ignores the mounted file, and falls back to the development default secret. The pin now lives in two places (`infra/week5/kustomization.yaml` and `infra/week7/urdnot/values.yaml`). Week 8 should make the pipeline pass the image tag to `helm upgrade`, which removes the hand-edit.
- **This branch was cut from `week-06-config-ingress`**, because Week 6 wasn't merged yet. Merge Week 6 first so this week's pull request shows only Week 7.
- **Namespaces created by `--create-namespace` have no Pod Security labels.** Week 5's Namespace manifest set `enforce: baseline` and `warn: restricted`. The runbook has the `kubectl label` command; a platform team would normally own namespaces.
- **dev and staging use the same secrets locally**, because both installs read one `values-local.yaml`. Real environments get separate secrets.
- **The Postgres and Redis image pins are now in three places** (`compose.yaml`, `infra/week5`, `values.yaml`). Dependabot updates only the first.
- **Changing `postgres.storage` or `redis.storage` after install fails**: `volumeClaimTemplates` are immutable.
- **`POSTGRES_PASSWORD` still doesn't trigger a rollout** when changed, and changing it on an existing database is still unsafe (Weeks 5 and 6).
- **No chart tests or values schema.** `helm test` hooks and a `values.schema.json` (which rejects a mistyped value at install time) are the usual next steps for a chart with more users.
- **The chart is not published.** The commands are in the runbook.
