# Week 5: Running urdnot-api on Kubernetes

Week 5 moves the Week 3 stack from Compose to Kubernetes. The image comes from the Week 4 pipeline, `ghcr.io/torehan06/urdnot-api`, pinned to `sha-0b1eec1` plus its digest. The manifests are plain YAML assembled with Kustomize, in [`infra/week5/`](../infra/week5/). The runbook is [infra/week5/README.md](../infra/week5/README.md). CI now deploys every change to a throwaway kind cluster and runs the same smoke test as the Compose job. No application code changed.

## What was built

| Object | Kind | Notes |
| --- | --- | --- |
| `urdnot` | Namespace | Pod Security: `baseline` enforced, `restricted` warned |
| `urdnot-api` | Deployment, 2 replicas | Startup, liveness and readiness probes, rolling update with `maxUnavailable: 0`, non-root, read-only root filesystem |
| `urdnot-api` | Service (ClusterIP), port 80 → 8000 | The only way to reach the API; use a port-forward from outside |
| `urdnot-api` | PodDisruptionBudget | `minAvailable: 1` |
| `postgres` | StatefulSet, 1 replica + headless Service | Same pinned image as Compose, 1 Gi PVC `data-postgres-0` |
| `redis` | StatefulSet, 1 replica + headless Service | Same pinned image as Compose, 512 Mi PVC `data-redis-0` |
| `urdnot-config-<hash>` | ConfigMap (generated) | `CLAN_NAME`, `LOG_LEVEL` |
| `urdnot-secrets-<hash>` | Secret (generated) | `POSTGRES_PASSWORD`, `CLAN_SECRET`, from the git-ignored `infra/week5/secrets.env` |

The mapping from Compose to Kubernetes:

| Compose (Week 3) | Kubernetes (Week 5) |
| --- | --- |
| service `api` | Deployment + Service `urdnot-api` |
| service name as hostname (`postgres`) | Service name as DNS name (`postgres`, in full `postgres.urdnot.svc.cluster.local`) |
| `.env` interpolation | Secret generated from `secrets.env`, injected with `secretKeyRef` |
| named volume `pgdata` | PVC from the StatefulSet's `volumeClaimTemplates` |
| image `HEALTHCHECK` (`/healthz`) | `livenessProbe` on `/healthz` (Kubernetes ignores the image `HEALTHCHECK`) |
| `depends_on: condition: service_healthy` | Nothing. The API starts on its own and stays out of the Service until `/readyz` passes |
| `ports: 127.0.0.1:8000:8000` | ClusterIP Service + `kubectl port-forward` |
| `stop_grace_period: 30s` | `terminationGracePeriodSeconds: 30` + a 5 s `preStop` sleep |
| `restart: on-failure` | The kubelet restarts containers; the controllers replace lost Pods |

## Why each choice was made

- **Kustomize, not Helm.** Kustomize is built into `kubectl` (`apply -k`). The files stay plain, valid Kubernetes YAML that you can read top to bottom. Helm's templating pays off when one chart has many users and many knobs. Here there's one deployment, and a template language would hide the manifests this week is meant to teach. Kustomize still gives three things plain `kubectl apply -f` doesn't: one namespace for everything, generated ConfigMaps and Secrets, and an image override (used by CI).
- **Generated ConfigMap and Secret, with a hash suffix.** Pods read environment variables only when they start. If you edited a fixed-name Secret, running Pods would keep the old values until something restarted them. Kustomize names it `urdnot-secrets-<hash of contents>` and rewrites every reference. A changed value changes the Pod template, and the Deployment rolls out by itself. Old ConfigMaps and Secrets are left behind; `kubectl apply --prune` or an occasional cleanup removes them.
- **Secrets come from a git-ignored file, not from the repo.** This is the same pattern as `.env` in Week 3. A Kubernetes Secret is only base64-encoded, not encrypted, so committing one would leak it. The alternatives (Sealed Secrets, External Secrets with a cloud secret manager, SOPS) need extra controllers or a cloud account. They are a production upgrade, not a Week 5 need.
- **`DATABASE_URL` is assembled with `$(POSTGRES_PASSWORD)`.** Kubernetes expands `$(VAR)` in `value:` from variables defined earlier in the same container's `env` list. The password is stored once, in the Secret, and not duplicated in a second Secret key that could drift. It must stay URL-safe (hex), as in Week 3.
- **The API is a Deployment and the databases are StatefulSets.** API Pods are interchangeable and keep no state, so a Deployment can replace them freely. Postgres and Redis need the same disk after every restart. A StatefulSet gives the Pod a stable name (`postgres-0`) and a PVC per replica that outlives the Pod and the StatefulSet itself. One replica each: replicating Postgres means streaming replication and failover, which is an operator's job (CloudNativePG, for example), not something to fake with `replicas: 2`.
- **Headless Services for the databases.** `clusterIP: None` makes the DNS name resolve straight to the Pod IP, with no virtual IP or kube-proxy load balancing in between, which a single database Pod doesn't need. It is also what gives StatefulSet Pods their own DNS names.
- **Three probes, each answering a different question.**
  - `startupProbe` on `/healthz`: has the process started? It allows up to 30 s and holds the other two probes back until it passes, so a slow start isn't killed by liveness.
  - `livenessProbe` on `/healthz`: is the process wedged? A failure restarts the container. `/healthz` checks no dependencies, so a Postgres outage never triggers an API restart loop. This is the Week 3 rule (liveness never uses `/readyz`), applied to Kubernetes.
  - `readinessProbe` on `/readyz`: should this Pod receive traffic? A failure takes the Pod out of the Service's endpoints without restarting it. When Postgres is down, every API Pod goes unready and the Service has no endpoints. That is honest: the app can't serve records. It replaces Compose's `depends_on`. Startup order doesn't matter, because the API isn't sent traffic until its dependencies answer.
  - Postgres and Redis reuse the Compose checks (`pg_isready -h 127.0.0.1`, `redis-cli ping | grep PONG`) for readiness, with a plain TCP check for liveness. A slow query must not get the database killed.
- **Zero-capacity-loss rollouts.** `maxSurge: 1, maxUnavailable: 0` means a new Pod must be Ready before an old one is stopped. With 2 replicas, at least 2 Pods serve throughout a rollout. `revisionHistoryLimit: 5` keeps old ReplicaSets for `kubectl rollout undo`.
- **Graceful shutdown: `preStop` sleep, then SIGTERM.** When a Pod is deleted, Kubernetes removes it from the Service's endpoints and starts the shutdown at the same time. kube-proxy on each node takes a moment to notice, so for a short window new requests can still arrive at a Pod that's shutting down. The 5 s `preStop` sleep delays SIGTERM until routing has caught up. After that, the app's own graceful shutdown (Week 1) drains in-flight requests. The total stays under the 30 s grace period, even with a 10 s `/api/charge`. This uses the built-in `sleep` action, so the image doesn't need a `sleep` binary.
- **Image pinned by tag and digest.** `sha-0b1eec1` tells a person which commit is running. The `@sha256:…` digest tells the kubelet exactly which bytes to run, even if the tag were pushed again. The digest is of the multi-arch index, so arm64 and amd64 nodes each pull their own image. The package is public, so no `imagePullSecret` is needed (checked with an anonymous registry token).
- **Hardened API Pod.** `runAsNonRoot` with the Dockerfile's numeric UID 10001, `readOnlyRootFilesystem` with a small `emptyDir` at `/tmp`, all capabilities dropped, no privilege escalation, `RuntimeDefault` seccomp, and no service-account token (the app never calls the Kubernetes API). The API Pod meets the `restricted` Pod Security profile.
- **The databases run the stock images, so the namespace enforces `baseline`.** The official Postgres and Redis entrypoints start as root, fix volume ownership, and drop to their service user, which `restricted` forbids. Running them as non-root would need an init step or a storage class that applies `fsGroup` (hostPath-based local storage doesn't), and that changes from cluster to cluster. `baseline` is enforced, `restricted` is warned on every apply so it stays visible, and the containers still can't escalate privileges.
- **Requests and limits.** Requests reserve capacity for scheduling. Memory limits stop a leak from taking the node down: the container is OOM-killed and restarted instead. The API's CPU limit is one core, because one Python process mostly uses one core anyway (GIL). The databases have no CPU limit, so throttling can't stall them. These numbers are starting points; Week 6+ metrics should drive them.
- **PodDisruptionBudget.** `kubectl drain` and cluster upgrades respect it, evicting at most one API Pod at a time. It doesn't help against crashes (involuntary disruptions).
- **No Ingress, NetworkPolicy, or HorizontalPodAutoscaler yet.** Each needs a controller or a CNI plugin that differs between clusters (an Ingress controller, a policy-enforcing CNI, metrics-server). The curriculum covers autoscaling and monitoring later. `port-forward` is enough to reach the app this week.
- **CI deploys to kind instead of only linting the YAML.** A schema check like kubeconform would catch typos, but not a wrong Service name in `DATABASE_URL`, a probe on the wrong port, or a security context the image can't run under. The `k8s` job builds the image from the commit under test, loads it into a kind node, applies the real manifests through a small overlay that only swaps the image, waits for all three rollouts, and runs `scripts/smoke-test.sh` through a port-forward. `publish` now needs `k8s` too, so an image that can't run on Kubernetes is never published.

## Verification

Run on 2026-11-09, in two places: in CI on kind (amd64, the image built from the commit) and locally on Rancher Desktop's k3s (arm64, the pinned GHCR image). Both are described below.

### CI on kind

The deployment ran in GitHub Actions on kind, Kubernetes v1.37.0 ([run 42634277728](https://github.com/Torehan06/urdnot/actions/runs/42634277728), `k8s` job, 1 min 32 s including cluster creation and the image build).

```text
$ kubectl kustomize infra/week5 | grep image:          # with a dummy secrets.env
image: ghcr.io/torehan06/urdnot-api:sha-0b1eec1@sha256:cea8c200…1eef
image: postgres:18.6-trixie@sha256:5a5a…9722
image: redis:8.10.2-trixie@sha256:97ec…9901

$ kubectl kustomize .k8s-ci | grep 'image: urdnot'    # the CI overlay
image: urdnot-api:ci                                   # tag replaced, base digest dropped
```

The `ghcr.io/torehan06/urdnot-api:sha-0b1eec1` index resolved anonymously (HTTP 200, digest `sha256:cea8c200…`). That confirms the package is public and the pinned digest is correct.

From the `k8s` job log:

```text
namespace/urdnot created
configmap/urdnot-config-g7gc84mmkf created
secret/urdnot-secrets-425c2t6mg8 created
...
Warning: would violate PodSecurity "restricted:latest": ... container "postgres" ...
Warning: would violate PodSecurity "restricted:latest": ... container "redis" ...
partitioned roll out complete: 1 new pods have been updated...      # postgres, ~12 s
Waiting for deployment "urdnot-api" rollout to finish: 1 of 2 updated replicas are available...
deployment "urdnot-api" successfully rolled out
ok   /healthz 200
ok   /readyz 200
ok   storage=postgres, counter=redis
ok   POST without secret 403
ok   POST with secret 201
ok   record smoke-1794234338-9617 read back
ok   headbutts 1 -> 2
ok   /metrics exposes http_requests_total
smoke test passed against http://127.0.0.1:8000
```

The Pod Security warnings name only `postgres` and `redis`. The API Pod passes the `restricted` profile, which confirms that `runAsNonRoot`, `readOnlyRootFilesystem` and the dropped capabilities work with the image. `storage=postgres, counter=redis` proves the Service DNS names and the `$(POSTGRES_PASSWORD)` expansion in `DATABASE_URL` work, and that the app didn't fall back to memory.

### Locally on k3s (arm64)

Rancher Desktop 1.23.1, k3s v1.36.2, Apple silicon, `local-path` storage class. Every `kubectl` command used `--context rancher-desktop` explicitly.

**Resources first.** Rancher Desktop's default VM (2 GB RAM, 2 CPUs) was too small. Postgres never became ready, the API server stopped answering (`TLS handshake timeout`), and `free -m` inside the VM showed 154 MB available with no swap and a load average of 5.6. After resizing the VM to 4 GB and 4 CPUs (`rdctl set --virtual-machine.memory-in-gb=4 --virtual-machine.number-cpus=4`), everything fit, with about 2.4 GB left over. The sum of this week's memory requests is only 576 Mi, but k3s itself, Traefik and the page cache need the rest.

```text
$ kubectl apply -k infra/week5            # same output and Pod Security warnings as in CI
Pulled image "ghcr.io/torehan06/urdnot-api:sha-0b1eec1@sha256:cea8c200…" in 32.3s. Image size: 58356093 bytes.
imageID: docker-pullable://ghcr.io/torehan06/urdnot-api@sha256:cea8c200…   # the arm64 variant, resolved from the index

$ BASE_URL=http://127.0.0.1:18000 scripts/smoke-test.sh   # through kubectl port-forward
ok   /healthz 200
ok   /readyz 200
ok   storage=postgres, counter=redis
...
smoke test passed against http://127.0.0.1:18000
```

That confirms the pinned published image itself runs, on arm64, without an `imagePullSecret`.

**Zero-downtime rollout.** A `curl` loop ran in a Pod inside the namespace, calling `http://urdnot-api/api/hollows` every 50 ms for 75 s. `kubectl rollout restart deployment/urdnot-api` ran in the middle of it:

```text
deployment "urdnot-api" successfully rolled out
ok=1096 bad=0
```

Not a single failed request while both Pods were replaced. This is the combination of `maxUnavailable: 0` (a new Pod is Ready before an old one stops), readiness gating, the 5 s `preStop` sleep, and the app's graceful SIGTERM handling.

**Postgres outage** (`kubectl scale statefulset/postgres --replicas=0`, then back to 1):

```text
--- during outage
urdnot-api-54fb5758b9-h8jnm   0/1   Running   0     104s
urdnot-api-54fb5758b9-t5bj4   0/1   Running   0     101s
urdnot-api-54fb5758b9-h8jnm ready=false        # EndpointSlice: no Pod gets traffic
urdnot-api-54fb5758b9-t5bj4 ready=false
--- after recovery
urdnot-api-54fb5758b9-h8jnm   1/1   Running   0     2m3s
urdnot-api-54fb5758b9-t5bj4   1/1   Running   0     2m
```

Both API Pods went unready and were taken out of the Service, but **0 restarts**: liveness checks `/healthz`, which has no dependencies. They came back on their own within 15 s of Postgres being Ready. The record written by the smoke test before the outage was still returned by `GET /api/hollows` afterwards, because the new `postgres-0` reattached the same PVC (`data-postgres-0`).

**Not verified:** a multi-node cluster (the topology spread constraint and PodDisruptionBudget were never exercised by a real drain), and a Redis outage (same mechanism as Postgres, not run separately).

## What you should be able to explain in an interview

- Pod, ReplicaSet, Deployment, StatefulSet: who creates whom, and why the API and the database use different controllers.
- How a Service finds its Pods (label selector → EndpointSlices), and what a headless Service changes.
- The difference between startup, liveness and readiness probes, and what goes wrong if liveness checks a dependency.
- What happens, step by step, between `kubectl delete pod` and the process exiting (endpoint removal, `preStop`, SIGTERM, grace period, SIGKILL).
- Why a Secret is not encryption, and the options for keeping Secret values out of git.
- What a PVC is, who creates the PersistentVolume, and what happens to the data when you delete a StatefulSet versus a namespace.
- Requests vs limits: what the scheduler uses, and what happens when a container exceeds each one.

## Test your understanding

1. Stop Postgres with `kubectl -n urdnot scale statefulset/postgres --replicas=0`. What do `kubectl get pods` and `kubectl get endpointslices` show for the API? Do the API Pods restart? Why not?
2. Change `LOG_LEVEL` in `kustomization.yaml` to `debug` and run `kubectl apply -k`. Why does a rollout start, and what would happen if the ConfigMap had a fixed name?
3. Run `kubectl -n urdnot rollout restart deployment/urdnot-api` while running `kubectl get pods -w` in another terminal. In what order are Pods created and terminated? Which setting decides that?
4. Your port-forward broke during that restart. Why? What would have kept working: a client inside the cluster calling `http://urdnot-api`, or the port-forward?
5. Delete the `postgres-0` Pod. Where does the new Pod get its data from? What if you delete the StatefulSet instead? And the namespace?
6. Why can't the API Pod write to `/opt/urdnot/app`, and why can it write to `/tmp`?
7. The CI overlay sets only `newName` and `newTag`. Render it with `kubectl kustomize` and compare the API image line to the base. What happened to the digest, and why is that what CI needs?

## Assumptions and open items

- **Rancher Desktop needs at least 4 GB for this stack.** With the default 2 GB VM, the node ran out of memory and the API server stopped responding (see Verification). Other local clusters (kind, minikube) need similar headroom.
- **The CI rollout check is functional only.** CI's smoke test goes through a port-forward, which tunnels to a single Pod and breaks when that Pod goes away, so CI doesn't test zero-downtime rollouts. That was tested locally with an in-cluster client (see Verification). Adding the same loop to the `k8s` job would make it a regression test.
- **Redis runs without AUTH**, as in Compose. Only Pods in the cluster can reach it, but without a NetworkPolicy that means *every* Pod in the cluster. A NetworkPolicy allowing only `urdnot-api` to reach `postgres:5432` and `redis:6379` is the next step once the cluster's CNI enforces policies.
- **Changing `POSTGRES_PASSWORD` in `secrets.env` breaks the API on an existing cluster.** The Postgres image reads it only when initializing an empty volume. Rotate it with `ALTER ROLE` first, then update the Secret.
- **Old generated ConfigMaps and Secrets pile up** with each change. They're harmless but not cleaned up.
- **Dependabot doesn't update `infra/week5`.** Its `docker-compose` updater doesn't read Kubernetes YAML, so the Postgres and Redis pins here must be bumped together with `compose.yaml`. The API pin is bumped by hand after each publish. Automating that (a CI job that opens a PR, or Argo CD Image Updater) is a GitOps topic for a later week.
