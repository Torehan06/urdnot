# Week 6: config, storage and Ingress runbook

A Kustomize overlay on top of [infra/week5](../week5/README.md). It adds three things and copies nothing. Design notes are in [docs/week-06.md](../../docs/week-06.md).

| File | Contents |
| --- | --- |
| `kustomization.yaml` | `../week5` as the base, fixed-name `urdnot-secrets`, namespace and labels for the new objects |
| `api-patch.yaml` | Mounts `CLAN_SECRET` as a file and sets `CLAN_SECRET_FILE`, instead of an env var |
| `ingress.yaml` | Routes Host `urdnot.localhost` to the `urdnot-api` Service |

Secrets still come from `infra/week5/secrets.env` (git-ignored). Kustomize only lets a kustomization read files in its own directory, and the base already owns that file.

## Requirements

- **An Ingress controller.** k3s (Rancher Desktop) ships Traefik as the default IngressClass. On kind or minikube, install one first and change `ingressClassName` in `ingress.yaml`.
- **An image with `CLAN_SECRET_FILE` support.** The pin in `infra/week5/kustomization.yaml` (`sha-66e9028`, bumped in Week 8) has it. The Week 5 pin (`sha-0b1eec1`) did not: it would ignore the file and fall back to the development default `shiagur`.

## Deploy

```sh
kubectl config current-context                  # must be the local cluster
kubectl apply -k infra/week6
kubectl -n urdnot rollout status deployment/urdnot-api
kubectl -n urdnot get pods,svc,ingress,pvc
```

To test this branch before an image is published, build into Rancher Desktop's Docker. k3s uses the same image store, so no registry is needed:

```sh
docker --context rancher-desktop build -t urdnot-api:week6-local .
# .k8s-local/ is git-ignored: a personal overlay that only swaps the image,
# like the one CI creates
mkdir -p .k8s-local
cat > .k8s-local/kustomization.yaml <<'EOF'
resources: [../infra/week6]
images:
  - name: ghcr.io/torehan06/urdnot-api
    newName: urdnot-api
    newTag: week6-local
EOF
kubectl apply -k .k8s-local
```

The first apply over a Week 5 stack restarts Postgres once. Its Pod template now references `urdnot-secrets` instead of `urdnot-secrets-<hash>`. The data is on the PVC and survives.

## Smoke test through the Ingress

No port-forward this time. `*.localhost` resolves to 127.0.0.1, and Rancher Desktop forwards port 80 to Traefik:

```sh
curl -i http://urdnot.localhost/healthz          # 200, from urdnot-api
curl -i http://other.localhost/healthz           # 404, from Traefik: no rule matches this Host
set -a; . infra/week5/secrets.env; set +a
BASE_URL=http://urdnot.localhost scripts/smoke-test.sh
```

## Rotate CLAN_SECRET without a restart

```sh
# edit CLAN_SECRET in infra/week5/secrets.env, then:
kubectl apply -k infra/week6        # secret/urdnot-secrets configured, deployment unchanged
kubectl -n urdnot get pods          # same Pods, no restarts
```

The kubelet rewrites the mounted file within about a minute. The app reads it on every request, so the new value takes effect without a rollout. Rotating `POSTGRES_PASSWORD` this way doesn't work; see the Week 5 notes.

## Change a ConfigMap value (rolls the Pods)

```sh
# edit LOG_LEVEL in infra/week5/kustomization.yaml, then:
kubectl apply -k infra/week6        # new urdnot-config-<hash>, deployment configured
kubectl -n urdnot rollout status deployment/urdnot-api
```

## Inspect storage

```sh
kubectl -n urdnot get pvc
kubectl get pv                      # reclaim policy, storage class, bound claim
kubectl get storageclass
kubectl get statefulset -n urdnot -o custom-columns=NAME:.metadata.name,RETENTION:.spec.persistentVolumeClaimRetentionPolicy
```

## Remove

`kubectl delete -k infra/week6` deletes the `urdnot` namespace, which deletes the PVCs. `local-path` has `reclaimPolicy: Delete`, so the PVs and the data on disk go too. To keep the data, set the PV to `Retain` first:

```sh
kubectl patch pv <pv-name> -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
```

To go back to Week 5, `kubectl delete -n urdnot ingress/urdnot-api`, then `kubectl apply -k infra/week5`.
