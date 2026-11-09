# Week 5: Kubernetes runbook

Deploys `urdnot-api` (2 replicas), Postgres and Redis into the `urdnot` namespace with Kustomize, which is built into `kubectl`. Design notes are in [docs/week-05.md](../../docs/week-05.md).

| File | Contents |
| --- | --- |
| `kustomization.yaml` | Namespace, labels, generated ConfigMap and Secret, API image pin |
| `namespace.yaml` | `urdnot` namespace with Pod Security labels |
| `api.yaml` | `urdnot-api` Service, Deployment, PodDisruptionBudget |
| `postgres.yaml` | Headless Service and StatefulSet with a 1 Gi PVC |
| `redis.yaml` | Headless Service and StatefulSet with a 512 Mi PVC |
| `secrets.env.example` | Template for the git-ignored `secrets.env` |

## Deploy

Any cluster works: Rancher Desktop's k3s, kind, minikube. The image `ghcr.io/torehan06/urdnot-api` is public, so no `imagePullSecret` is needed. Always check the context first. `kubectl` acts on whichever cluster is current, and that can be a real cloud cluster.

```sh
kubectl config current-context

# Once per cluster: throwaway values, like .env for Compose
{
  echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
  echo "CLAN_SECRET=$(openssl rand -hex 24)"
} > infra/week5/secrets.env

kubectl apply -k infra/week5
kubectl -n urdnot rollout status statefulset/postgres
kubectl -n urdnot rollout status deployment/urdnot-api
kubectl -n urdnot get pods,svc,pvc
```

## Smoke test

The Services are `ClusterIP`, so nothing is reachable from outside the cluster. A port-forward tunnels one local port to one Pod behind the Service:

```sh
kubectl -n urdnot port-forward svc/urdnot-api 8000:80 &
set -a; . infra/week5/secrets.env; set +a
BASE_URL=http://127.0.0.1:8000 scripts/smoke-test.sh
```

CI runs exactly this against a kind cluster (the `k8s` job in [ci.yml](../../.github/workflows/ci.yml)).

## Day-2 operations

```sh
# Roll out a new image (CI publishes sha-<7> tags from main)
#   edit newTag in kustomization.yaml, then:
kubectl apply -k infra/week5
kubectl -n urdnot rollout status deployment/urdnot-api

# Roll back to the previous ReplicaSet
kubectl -n urdnot rollout undo deployment/urdnot-api

# Logs of every API replica, prefixed with the Pod name
kubectl -n urdnot logs -l app.kubernetes.io/name=urdnot-api --prefix -f

# Which Pods currently receive traffic (only Ready ones are listed)
kubectl -n urdnot get endpointslices -l kubernetes.io/service-name=urdnot-api

# A shell-less debug check from inside the namespace
kubectl -n urdnot run curl --rm -it --restart=Never --image=curlimages/curl -- \
  curl -fsS http://urdnot-api/readyz
```

## Remove

```sh
# Stop everything but keep the data (the PVCs stay)
kubectl -n urdnot delete deployment/urdnot-api statefulset/postgres statefulset/redis

# Delete everything, data included
kubectl delete -k infra/week5
```

Deleting a StatefulSet never deletes the PVCs its `volumeClaimTemplates` created. `kubectl delete -k` is different: it also deletes the `urdnot` namespace, and deleting a namespace deletes everything in it, PVCs included.
