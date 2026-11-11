# Week 6: Configuration, storage and traffic routing

Week 6 builds on the Week 5 Kubernetes deployment. It doesn't change the Week 5 manifests. It adds a Kustomize overlay, [`infra/week6/`](../infra/week6/), with three additions:

1. **Ingress.** `http://urdnot.localhost` reaches the API through the cluster's Ingress controller. Week 5 needed a `kubectl port-forward`.
2. **Configuration that updates in place.** `CLAN_SECRET` is now a file from a Secret volume, re-read by the app on every request. A rotated secret takes effect in running Pods without a restart. Settings that are env vars (the ConfigMap) still roll out new Pods, on purpose.
3. **Storage, made visible.** The PVCs from Week 5 are unchanged, but their PersistentVolumes, StorageClass, reclaim policy and retention policy were inspected and are documented below.

The app got one small feature for this: an optional `CLAN_SECRET_FILE` setting. The runbook is [infra/week6/README.md](../infra/week6/README.md).

## What was built

| Change | Where | Notes |
| --- | --- | --- |
| `CLAN_SECRET_FILE` | `app/main.py` | Read on every request and takes precedence over `CLAN_SECRET`. A missing or empty file returns 503 (fails closed). Three new tests. |
| Overlay on Week 5 | `infra/week6/kustomization.yaml` | `resources: [../week5]`, nothing copied |
| `urdnot-secrets` with a fixed name | same | `secretGenerator` with `behavior: merge` and `disableNameSuffixHash: true` |
| Secret volume for the API | `infra/week6/api-patch.yaml` | Only the `CLAN_SECRET` key is mounted, at `/etc/urdnot/secrets/clan-secret`, mode `0440`, `fsGroup: 10001`. The `CLAN_SECRET` env var is removed. |
| Ingress `urdnot-api` | `infra/week6/ingress.yaml` | Host `urdnot.localhost`, path `/` (Prefix) → Service `urdnot-api:http`, `ingressClassName: traefik` |
| CI | `.github/workflows/ci.yml` | The `k8s` job deploys `infra/week6` instead of `infra/week5`, and checks that the Ingress lands in `urdnot` |

## How configuration reaches a Pod

This is the core of the week. There are three ways for a Pod to consume a ConfigMap or Secret, and they behave differently when the object changes:

| How the Pod consumes it | What happens when the object changes |
| --- | --- |
| Env var (`env.valueFrom`, `envFrom`) | Nothing. The process environment is fixed when the container starts. New values arrive only with a new Pod. |
| Volume, whole directory | The kubelet rewrites the files in the running Pod, typically within a minute (its sync period plus its cache TTL). The app must re-read the file. |
| Volume with `subPath` | Nothing. A `subPath` mount is a bind mount of one file at start, so it never sees updates. |

Week 6 uses each where it fits:

- **`CLAN_NAME`, `LOG_LEVEL` (ConfigMap, env vars).** These keep the Week 5 hash suffix. A change creates `urdnot-config-<new hash>`, which changes the Pod template, so the Deployment rolls out by itself. Without the suffix, editing the ConfigMap would change nothing visible: the running Pods keep the old values until they happen to restart. Then different replicas would run different configurations. `LOG_LEVEL` couldn't be reloaded live anyway: Uvicorn and the logger read it once at startup.
- **`CLAN_SECRET` (Secret, volume, fixed name).** An in-place update only works if the name stays the same: a hash-suffixed name would be a new object, and the existing Pods would keep mounting the old one. So the overlay switches the suffix off for `urdnot-secrets`. The kubelet then updates the file, and `clan_secret()` in the app reads it on the next request.
- **`POSTGRES_PASSWORD` (same Secret, env var).** This stays an env var for both Postgres and the API. Rotating it isn't a config change: Postgres reads it only when it initializes an empty volume, so the database password must be changed with `ALTER ROLE` first (see Week 5). Losing the hash suffix means a change to it no longer forces a rollout. That is acceptable here, but it is a trade-off (see open items).

The kubelet updates a Secret volume atomically. The visible file is a symlink into a timestamped directory, and an update writes a new directory and then swaps one symlink. A reader sees either the whole old file or the whole new one, never a half-written file:

```text
$ kubectl -n urdnot exec <api-pod> -- ls -la /etc/urdnot/secrets/
drwxrwsrwt 3 root urdnot  100 .
drwxr-sr-x 2 root urdnot   60 ..2026_11_11_18_26_35.2553071492
lrwxrwxrwx 1 root urdnot   32 ..data -> ..2026_11_11_18_26_35.2553071492
lrwxrwxrwx 1 root urdnot   18 clan-secret -> ..data/clan-secret
```

## Why each choice was made

- **An overlay, not a copy of Week 5.** `infra/week6` lists only what changes. A copied `infra/week6` would drift from `infra/week5` the first time someone bumped an image pin in only one of them. It's also the shape a Helm chart's per-environment values take next week, without a template language yet.
- **The overlay repeats `namespace:` and `labels:`.** A kustomization's namespace and label transformers apply only to that kustomization's own resources and whatever it inherits. The base's `namespace: urdnot` does not reach a file the overlay adds. The first local deploy created the Ingress in `default`, and Traefik logged `service not found` because there was no `urdnot-api` Service there. Setting `namespace: urdnot` in the overlay fixed it, and CI now checks for the Ingress in `urdnot`.
- **The secret as a file, not only an env var.** Besides live rotation, a file is harder to leak. Env vars show up in `kubectl exec … env`, in `/proc/<pid>/environ`, in crash reports and debug dumps, and they are inherited by every child process. Only the `CLAN_SECRET` key is projected (`items:`), so `POSTGRES_PASSWORD` is not on the API's filesystem. The Postgres image supports the same convention (`POSTGRES_PASSWORD_FILE`). That's a later hardening step, not needed for this week.
- **`fsGroup: 10001` and `defaultMode: 0440`.** Secret volume files belong to root. Because the API runs as UID 10001, the default mode `0644` would work but would make the file world-readable inside the container. `fsGroup` makes the volume group-owned by 10001, and `0440` lets only root and that group read it. Verified in the Pod: `uid=10001(urdnot)` can read it, and the files are `root urdnot`.
- **The app fails closed.** If `CLAN_SECRET_FILE` is set but the file is missing, unreadable or empty, `POST /api/hollows` returns 503. Without this, an empty file would make an empty `X-Clan-Secret` header valid (`compare_digest(b"", b"")` is true). Falling back to `CLAN_SECRET` or to the development default would silently accept a secret the operator meant to replace. Reads stay cheap: one small file read per write request.
- **Traefik, the controller k3s already runs.** Ingress is a standard API, but it does nothing on its own. A controller watches Ingress objects and configures a proxy. k3s installs Traefik as the default IngressClass, and Rancher Desktop forwards host ports 80 and 443 to it. The community `ingress-nginx` controller, the usual "NGINX Ingress" in tutorials, was retired by Kubernetes SIG Network: maintenance ended in March 2026 after an announcement in November 2025, and there are no more releases or security fixes. Installing it would teach a dead end. The manifest uses only standard `networking.k8s.io/v1` fields, so moving to another controller (F5's NGINX Ingress Controller, HAProxy, Traefik elsewhere) means changing `ingressClassName`. The longer-term replacement for Ingress is the Gateway API (`Gateway` and `HTTPRoute`). It's worth knowing about, but this week's topic is Ingress.
- **`ingressClassName` set explicitly.** Relying on the default class works on this cluster, but a cluster with two controllers could give the route to the wrong one, or to neither. An explicit class fails visibly instead.
- **Host-based routing on `urdnot.localhost`.** RFC 6761 reserves `.localhost` for loopback. curl and browsers resolve every `*.localhost` to 127.0.0.1 without an `/etc/hosts` entry or a real domain. The controller picks the backend by the HTTP `Host` header. Requests for any other host get Traefik's 404, which proves the rule matches on the name and not just on the port.
- **No TLS this week.** It would mean a certificate for `urdnot.localhost` (self-signed or a local CA such as mkcert) and a `tls:` section with a Secret of type `kubernetes.io/tls`. In production this is cert-manager with Let's Encrypt on a real domain. It needs a public DNS name, which the course gets with Terraform in Week 10.
- **No storage manifest changes.** The Week 5 PVCs are already right for this stack: dynamic provisioning from the default StorageClass, `ReadWriteOnce`, one per StatefulSet replica. Week 6 makes the storage objects explicit and documents the risks (below) instead of adding unused objects. A hand-written PersistentVolume (static provisioning) only makes sense when an administrator pre-creates disks, which `local-path` does on demand.

## Storage: PV, PVC and StorageClass on this cluster

```text
$ kubectl get storageclass local-path
local-path (default)   rancher.io/local-path   Delete   WaitForFirstConsumer

$ kubectl get pv     # urdnot rows only
pvc-f5c9fe7d-…   data-postgres-0   1Gi     Delete   local-path   node lima-rancher-desktop
pvc-4b0d97e9-…   data-redis-0      512Mi   Delete   local-path   node lima-rancher-desktop
spec.local.path: /var/lib/rancher/k3s/storage/pvc-f5c9fe7d-…_urdnot_data-postgres-0

$ kubectl get statefulset -n urdnot  (persistentVolumeClaimRetentionPolicy)
postgres   whenDeleted:Retain whenScaled:Retain
redis      whenDeleted:Retain whenScaled:Retain

$ kubectl -n urdnot exec postgres-0 -- df -h /var/lib/postgresql
/dev/vda1   98G  7.0G   86G   8% /var/lib/postgresql
```

What this shows:

- **The chain is StorageClass → PVC → PV.** The StatefulSet's `volumeClaimTemplates` creates the PVC `data-postgres-0` (a request: 1 Gi, RWO). The `local-path` provisioner sees an unbound PVC of its class and creates a PV for it: a directory on the node. The PV then binds to that PVC one-to-one.
- **`WaitForFirstConsumer`.** The PV is created only once a Pod using the PVC is scheduled, so the directory lands on the node where the Pod runs. With `Immediate`, a node-local volume could end up on a node the Pod can't use.
- **The size is not enforced.** `df` shows the node's whole 98 GB disk, not 1 Gi. `local-path` is a plain directory without a quota. On a cloud disk (EBS, Persistent Disk) the volume really would be 1 Gi.
- **Two different "delete" policies.** `persistentVolumeClaimRetentionPolicy` on the StatefulSet decides whether deleting or scaling down the StatefulSet deletes the PVCs (here: no, `Retain`, the default). `reclaimPolicy` on the PV decides what happens to the data when the PVC is deleted (here: `Delete`, inherited from the StorageClass). Deleting the namespace deletes the PVCs, and with `Delete`, the PVs and the directories go too. That's why `kubectl delete -k` really destroys the data. Patching the PV to `Retain` keeps it.
- **The data survived a restart.** Applying Week 6 restarted `postgres-0`, because the Secret name in its template changed. The records written on 2026-11-09 were still returned afterwards, because the new Pod mounted the same PVC.

## Verification

Run on 2026-11-11, locally, on Rancher Desktop 1.23.1 with k3s v1.36.2 (arm64), Traefik as the default IngressClass. Every command used `--context rancher-desktop`. The image was built from this branch (`urdnot-api:week6-local`, via the git-ignored `.k8s-local` overlay), because the published pin predates `CLAN_SECRET_FILE`.

**Unit tests:** `python -m pytest app/tests -q` → `21 passed`. That includes the new `test_clan_secret_file_is_reread` (the file beats the env var, a trailing newline is ignored, a rewrite takes effect without restarting the app) and `test_clan_secret_file_fails_closed` (missing, empty, whitespace-only → 503).

**Rendering.** `kubectl kustomize infra/week6` renders one fixed-name `urdnot-secrets` Secret. The API and Postgres references were rewritten to it, the API container has `CLAN_SECRET_FILE` and no `CLAN_SECRET`, and the Ingress is in `urdnot`.

**Apply over the running Week 5 stack:**

```text
secret/urdnot-secrets created
deployment.apps/urdnot-api configured
statefulset.apps/postgres configured
ingress.networking.k8s.io/urdnot-api created
deployment "urdnot-api" successfully rolled out
```

**Host-based routing:**

```text
$ curl -s -o /dev/null -w '%{http_code}' http://urdnot.localhost/healthz   → 200
$ curl -s -o /dev/null -w '%{http_code}' http://other.localhost/healthz    → 404
10 requests to http://urdnot.localhost/  → 5 × urdnot-api-…-rkt5k, 5 × urdnot-api-…-zlzwk
```

The Ingress load-balances over both replicas. Traefik sends traffic straight to the Pod endpoints, not through the Service's virtual IP.

**Smoke test through the Ingress**, without a port-forward:

```text
$ BASE_URL=http://urdnot.localhost scripts/smoke-test.sh
ok   /healthz 200
ok   /readyz 200
ok   storage=postgres, counter=redis
ok   POST without secret 403
ok   POST with secret 201
...
smoke test passed against http://urdnot.localhost
```

`POST with secret 201` passing means the secret now arrives as a file. The env var is gone, so it can't have come from there.

**Live secret rotation.** `CLAN_SECRET` in `secrets.env` was changed and `kubectl apply -k` was run. Then the test polled `POST /api/hollows` with the new value (6 requests per round, so both replicas were hit):

```text
secret/urdnot-secrets configured
deployment.apps/urdnot-api unchanged
new secret accepted by every request after 9s
old secret now returns 403
pods before: urdnot-api-6c87ff846c-rkt5k:0 urdnot-api-6c87ff846c-zlzwk:0
pods after:  urdnot-api-6c87ff846c-rkt5k:0 urdnot-api-6c87ff846c-zlzwk:0
CLAN_SECRET env vars in pod: 0
uid=10001(urdnot) gid=10001(urdnot) groups=10001(urdnot)
```

The same Pods kept serving with 0 restarts. Restoring the original value took about 60 s the second time. The delay depends on where each kubelet is in its sync loop, so plan for "up to a minute or two", not "instant". The rotation test wrote a few `rotation` records to the Hollows table.

**`apply` noise.** Every re-apply reports the two StatefulSets and the PodDisruptionBudget as `configured`. `kubectl diff` shows no difference and no Pod restarts, so this is client-side apply bookkeeping, not drift.

**Not verified:**
- CI on kind with the new overlay. That runs when this branch is pushed. kind has no Ingress controller, so CI covers the secret file (through the smoke test) and the Ingress's namespace, but not routing.
- An image published from `main` with `CLAN_SECRET_FILE`. It doesn't exist until this is merged.
- Another Ingress controller than Traefik, and TLS.

## What you should be able to explain in an interview

- ConfigMap vs Secret: what's different (separate RBAC, Secret volumes live in memory (tmpfs) on the node, optional encryption at rest in etcd) and what isn't (both max 1 MiB, base64 is not encryption).
- The three ways to consume them (env, volume, `subPath`) and which ones see updates.
- Why a hash-suffixed name gives you rollouts on change, and why it rules out in-place file updates.
- Why an app needs to re-read a mounted file, and what "atomic update via symlink swap" buys you.
- Ingress object vs Ingress controller vs IngressClass, and what happens to an Ingress with no controller.
- How the controller picks a backend (`Host` header, path, `pathType`), and how that differs from a `NodePort` or `LoadBalancer` Service.
- StorageClass, PVC, PV: who creates which, `WaitForFirstConsumer`, and `reclaimPolicy` vs the StatefulSet's PVC retention policy.

## Test your understanding

1. Change `LOG_LEVEL` to `debug` in `infra/week5/kustomization.yaml` and apply `infra/week6`. Then change `CLAN_SECRET` in `secrets.env` and apply again. Why does only the first change roll the Pods? Watch `kubectl get pods -w` during both.
2. In `api-patch.yaml`, mount the secret with `subPath: clan-secret` at `/etc/urdnot/clan-secret` instead of the whole directory, and point `CLAN_SECRET_FILE` there. Rotate the secret. What happens, and why?
3. Run `kubectl -n urdnot exec deploy/urdnot-api -- env` and look for secret values. `CLAN_SECRET` is gone, but the database password is still there, twice (`POSTGRES_PASSWORD` and inside `DATABASE_URL`). Why is it harder to move to a file than `CLAN_SECRET` was?
4. Delete the Ingress (`kubectl -n urdnot delete ingress urdnot-api`) and curl `http://urdnot.localhost/` again. Who answers now, and with what status? What is the Service doing meanwhile?
5. Add a second rule for `admin.urdnot.localhost` with path `/metrics` only. Which requests reach the API through each host? What does `pathType: Exact` change?
6. Run `kubectl get pv`, then patch the Postgres PV to `Retain`. What would `kubectl delete -k infra/week6` now leave behind, and how would you reattach it to a new PVC?
7. Before this week, the Ingress landed in `default` (see "Why each choice was made"). What exactly did Kustomize do, and how would you have found the cause without Traefik's log?

## Assumptions and open items

- **Bump the API image pin after merging.** The pinned image (`sha-0b1eec1`) doesn't know `CLAN_SECRET_FILE`. With `infra/week6`, it would ignore the file and fall back to the development default `shiagur`. The pin lives in `infra/week5/kustomization.yaml` (both weeks use it) and must move to the first image CI publishes from `main` after this merge. Until then, deploy Week 6 only with a locally built image (runbook). CI isn't affected: it always tests the image built from the commit.
- **`POSTGRES_PASSWORD` no longer triggers a rollout** when it changes, because the Secret has a fixed name now. Changing it on a running cluster was already unsafe (Week 5), so nothing that worked before is lost. Splitting it into a second, hash-suffixed Secret would restore the rollout. It wasn't done, to keep one `secrets.env`.
- **The old `urdnot-secrets-<hash>` Secret from Week 5 is still in the cluster.** Nothing references it. `kubectl -n urdnot delete secret <name>` removes it.
- **The Ingress serves plain HTTP**, only on this machine. TLS is a later step (see "No TLS this week").
- **Port 80 must be free** on the host for Rancher Desktop to forward it to Traefik. If something else uses it, Traefik is still reachable on the NodePort (`kubectl -n kube-system get svc traefik`).
- **Rotating `CLAN_SECRET` has a window** of up to about a minute in which replicas can disagree: one already accepts the new secret, the other still the old one. Clients should switch over only after the change has spread to every replica, or the app could accept both values during the change (not implemented).
