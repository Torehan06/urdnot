# Week 9: scaling runbook

Turning the autoscaler on, putting load on the API with k6, and watching Pods being added and removed. Design notes and measurements are in [docs/week-09.md](../../docs/week-09.md).

| Path | Contents |
| --- | --- |
| `../week7/urdnot/templates/api.yaml` | The HorizontalPodAutoscaler (rendered when `api.autoscaling.enabled` is true) |
| `../week7/values-staging.yaml` | Turns it on for staging: 2 to 5 Pods |
| `load-test.js` | The k6 script: log in, then read the session and call `/api/charge` in a loop |
| `k6.yaml` | The Pod that runs it inside the cluster |
| `../../scripts/load-test.sh` | Starts the Pod, prints a timeline, judges the result |

## Requirements

- The staging release from the [Week 7](../week7/README.md) and [Week 8](../week8/README.md) runbooks, running an image that has `/api/session` (built from this week's code or later).
- metrics-server. k3s and Rancher Desktop ship it. Check:

```sh
kubectl top pods -n urdnot-staging        # prints CPU and memory, not an error
```

## Turn the autoscaler on

`values-staging.yaml` already sets `api.autoscaling.enabled: true`, so a normal deploy does it:

```sh
docker --context rancher-desktop build -t urdnot-api:week9-a --build-arg REVISION=week9-a .
IMAGE_REPOSITORY=urdnot-api scripts/deploy.sh staging week9-a
kubectl -n urdnot-staging get hpa urdnot-api
# NAME         REFERENCE               TARGETS       MINPODS   MAXPODS   REPLICAS
# urdnot-api   Deployment/urdnot-api   cpu: 9%/70%   2         5         2
```

`cpu: <unknown>/70%` for the first minute is normal: metrics-server has no reading for new Pods yet. If it stays, see Troubleshooting.

The first deploy that turns autoscaling on removes `replicas` from the Deployment. Kubernetes then falls back to 1 Pod for a few seconds until the autoscaler restores its minimum (see docs/week-09.md). Later deploys don't do this.

| Value (`api.autoscaling.*`) | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` (staging: `true`) | Render the HPA and leave `replicas` out of the Deployment |
| `minReplicas`, `maxReplicas` | 2, 5 | The range the HPA may choose from |
| `targetCPUUtilizationPercentage` | 70 | Average CPU use, as a percentage of `api.resources.requests.cpu` |
| `scaleDownStabilizationSeconds` | 300 | How long load must stay low before a Pod is removed |

## Run the load test

```sh
scripts/load-test.sh urdnot-staging
```

With the defaults it takes about 12 minutes: 30 s ramp-up, 3 min at 10 virtual users, then the wait for the Pods to go back to 2. It prints a line every 10 seconds:

```text
10:02:40  load       cpu   47% of request (target 70%)  pods 2 ready 2  hpa wants 2
```

and exits 1 if requests failed, a session was not recognised, the Pod count never went up, or it didn't come back down. Watch from a second terminal while it runs:

```sh
kubectl -n urdnot-staging get hpa urdnot-api -w
kubectl -n urdnot-staging get pods -l app.kubernetes.io/name=urdnot-api -w
kubectl -n urdnot-staging top pods
kubectl -n urdnot-staging describe hpa urdnot-api      # Events: why it scaled
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `VUS` | 10 | Virtual users at the peak |
| `RAMP_UP`, `HOLD`, `RAMP_DOWN` | `30s`, `3m`, `15s` | Length of each stage |
| `CHARGE_MS` | 100 | CPU milliseconds per `/api/charge` request |
| `THINK_MS` | 300 | Pause between one user's iterations |
| `EXPECT_SCALE_UP` | 1 | 0 = don't require a scale-up (light test, or no metrics-server) |
| `WAIT_SCALE_DOWN` | 1 | 0 = stop when k6 is done |
| `SCALE_DOWN_TIMEOUT` | 900 | Seconds to wait for the minimum |
| `NAMESPACE`, `KUBE_CONTEXT` | `urdnot-staging`, current | As in the other scripts |

A light run that only checks sessions across replicas (what CI does):

```sh
VUS=4 RAMP_UP=5s HOLD=20s RAMP_DOWN=5s CHARGE_MS=20 EXPECT_SCALE_UP=0 WAIT_SCALE_DOWN=0 scripts/load-test.sh urdnot-staging
```

## See the session move between Pods

```sh
curl -s -c /tmp/urdnot-cookies -X POST http://staging.urdnot.localhost/api/session \
  -H 'Content-Type: application/json' -d '{"name":"Wrex"}'
for i in 1 2 3 4 5 6; do curl -s -b /tmp/urdnot-cookies http://staging.urdnot.localhost/api/session; echo; done
```

`served_by` changes between Pod names while `visits` keeps counting: no Pod owns the session. Look at it in Redis (the key is a hash of the cookie, not the cookie):

```sh
kubectl -n urdnot-staging exec redis-0 -- redis-cli --scan --pattern 'urdnot-api:session:*'
kubectl -n urdnot-staging exec redis-0 -- redis-cli hgetall <key>
kubectl -n urdnot-staging exec redis-0 -- redis-cli ttl <key>
```

## See what breaks without Redis

Two processes on the laptop with no `REDIS_URL` stand in for two Pods with sessions in memory:

```sh
PORT=8001 python -m app & PORT=8002 python -m app &
curl -s -c /tmp/urdnot-cookies -X POST localhost:8001/api/session -H 'Content-Type: application/json' -d '{"name":"Wrex"}'
curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/urdnot-cookies localhost:8001/api/session   # 200
curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/urdnot-cookies localhost:8002/api/session   # 401
kill %1 %2
```

## Scale by hand

With the autoscaler on, `kubectl scale` is overruled at the next check (15 seconds) if the number is outside what the HPA wants. To pin a number, change `minReplicas` and `maxReplicas` through Helm. Without the autoscaler (dev), `api.replicaCount` is the number:

```sh
helm upgrade urdnot infra/week7/urdnot -n urdnot-dev \
  -f infra/week7/values-dev.yaml -f infra/week7/values-local.yaml --set api.replicaCount=3
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `cpu: <unknown>/70%` for more than two minutes | No metrics-server (`kubectl top pods` fails), or the container has no CPU request, so there is nothing to take a percentage of. `kubectl describe hpa` names the reason. |
| New Pods stay `Pending` | The node has no room for another CPU or memory *request*. `kubectl describe pod` shows `Insufficient cpu`. Lower `maxReplicas` or the request, or add a node. |
| Pods are added but latency doesn't improve | The load generator keeps connections open, so requests stay on the old Pods. `load-test.js` sets `noConnectionReuse` for this reason. |
| Pod count goes up and down repeatedly | The stabilization window is too short for the traffic pattern, or the target is close to the idle load. |
| k6 reports failed `session` checks | A replica doesn't see the session: `REDIS_URL` is missing or Redis is down (`/readyz` shows it). |
| The Pod count never drops | Wait for the stabilization window (5 minutes) plus one minute per Pod. `kubectl describe hpa` shows `ScaleDownStabilized` meanwhile. |
