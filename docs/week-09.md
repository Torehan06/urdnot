# Week 9: stateless replicas, horizontal scaling and load balancing

Week 9 makes the number of API Pods follow the load. Three things were needed. Sessions moved into Redis, so every replica can answer every request. A HorizontalPodAutoscaler (HPA) in the Helm chart adds Pods when CPU usage is high and removes them when it drops. A k6 load test produces that load and checks, at the same time, that no request fails and no session is lost while Pods come and go. The runbook is [infra/week9/README.md](../infra/week9/README.md).

## What was built

| Change | Where | Notes |
| --- | --- | --- |
| Sessions | `app/main.py`, `app/tests/test_api.py` | `POST`, `GET`, `DELETE /api/session` with a cookie. Stored in Redis when `REDIS_URL` is set, else in the process's memory. New variable `SESSION_TTL_SECONDS`. |
| Autoscaler | `infra/week7/urdnot/templates/api.yaml`, `values.yaml` | An `autoscaling/v2` HPA on CPU, behind `api.autoscaling.enabled`. Chart version 0.2.0. |
| Staging uses it | `infra/week7/values-staging.yaml` | 2 to 5 Pods at 70% CPU. dev stays at one fixed Pod. |
| CPU request | `infra/week7/urdnot/values.yaml` | API request raised from 100m to 250m; it is now the base of the HPA's percentage. |
| Load test | `infra/week9/load-test.js`, `infra/week9/k6.yaml`, `scripts/load-test.sh` | k6 as a Pod in the cluster. One command prints a timeline and fails if scaling or sessions don't work. |
| Smoke test | `scripts/smoke-test.sh` | Logs in, reads the session back from Redis, logs out. |
| CI | `.github/workflows/ci.yml`, job `k8s` | Checks that the HPA holds two Pods and runs a short k6 pass to prove both Pods know every session. |

## Stateful and stateless

A process is **stateful** when it keeps something in its own memory or on its own disk that a later request depends on. It is **stateless** when everything a request needs is in the request itself or in a shared store. Only stateless Pods are interchangeable, and only interchangeable Pods can be added, removed and replaced freely.

Sessions are the classic case. After login the server remembers who you are, and the browser sends a cookie with a random session ID on every request.

- **In the process's memory** (the app without `REDIS_URL`): only the replica that handled the login knows the session. With two replicas behind a load balancer, about half the requests reach the other one and get 401. A restart or a rollout logs everyone out. Scaling down logs out everyone whose session was on the removed Pod.
- **In Redis** (the app with `REDIS_URL`): the replica looks the ID up in Redis. Any replica can answer, and Pods can come and go.

What state the service has, and where it lives now:

| State | Where | Shared by all replicas |
| --- | --- | --- |
| Records (`/api/hollows`) | Postgres | Yes (since Week 3) |
| Counter (`/api/headbutts`) | Redis | Yes (since Week 3) |
| Sessions (`/api/session`) | Redis | Yes (this week) |
| `/metrics` counters, uptime | Process memory | No, on purpose: they describe one replica. Prometheus adds them up (Week 11). |

The state didn't disappear; it moved. The API tier is stateless *because* Postgres and Redis are stateful. That is why those two are StatefulSets with volumes and one replica each, and why the HPA targets only the API. Scaling a database is a different problem (replication, failover) and is not solved by adding Pods.

The other ways to handle sessions, and why they weren't used:

- **Sticky sessions**: the load balancer sends a user to the same Pod every time. No code change, but a Pod that is removed takes its sessions with it, and load spreads unevenly. It hides the state instead of moving it.
- **Signed tokens (JWT)**: the session data is in the cookie itself, signed by the server. No store at all, but a token can't be revoked before it expires without a store, and the curriculum asks for Redis.

### How the session works here

- `POST /api/session` creates a random 256-bit token (`secrets.token_urlsafe`) and sets it as the `urdnot_session` cookie: `HttpOnly` (page scripts can't read it), `SameSite=Lax`, limited to `/api`. There is no password; the exercise is about where the state lives.
- Redis holds a hash under `urdnot-api:session:<SHA-256 of the token>` with `name` and `visits`. The token itself is never stored, so someone who can read Redis can't copy a cookie out of it.
- `GET /api/session` runs `HGET`, `HINCRBY visits` and `EXPIRE` in one `MULTI`/`EXEC`. Each request pushes the expiry out again (`SESSION_TTL_SECONDS`, default one hour), and Redis deletes idle sessions by itself.
- The answer includes `served_by`, the hostname of the Pod. Repeating the request shows different Pods serving one session.
- If Redis is down, the session endpoints return 503, like the counter. They never fall back to memory: that would quietly bring the stateful behaviour back.

## Requests and limits

Each container can declare two numbers per resource.

| | Request | Limit |
| --- | --- | --- |
| Meaning | Reserved for the container | The most it may use |
| Used by | The scheduler: a Pod is placed only on a node with that much unreserved. The HPA: 100% CPU means "using exactly the request". | The kernel (cgroups), while the container runs |
| CPU above it | Allowed, if the node has spare CPU | The container is **throttled**: it is paused until the next 100 ms period. It gets slower, nothing crashes. |
| Memory above it | Allowed, but the Pod is among the first evicted when the node runs short | The container is **killed** (`OOMKilled`) and restarted |

CPU is compressible (you can wait for it), memory is not (it can't be taken back), which is why going over the limit has such different results.

The API's values and the reasons:

| | Value | Why |
| --- | --- | --- |
| CPU request | 250m | An idle Pod uses about 10m (probes). The request decides how much load one Pod carries before the HPA adds another: 70% of 250m is 175m. It also bounds the reservation: 5 Pods reserve 1.25 of the node's 4 cores. |
| CPU limit | 1 | One Python process runs on about one core at a time (GIL), so more can't be used. The limit mainly protects the neighbours on a shared node. |
| Memory request | 128Mi | Measured use is 48 to 61Mi, also under load. |
| Memory limit | 256Mi | A leak kills this container instead of starving the node. |

The request was 100m before this week. With that, 70% would have meant 70m, which is seven requests of 100 ms CPU per ten seconds: the HPA would have scaled on almost any traffic. **Request and target must be chosen together**, because the HPA only ever sees their ratio.

Requests and limits also set the Pod's QoS class: `Guaranteed` when they are equal, `Burstable` when the request is lower (the API, Postgres, Redis), `BestEffort` with neither. Under memory pressure the node evicts BestEffort first and Guaranteed last.

## How the HPA decides

Every 15 seconds the HPA controller reads the CPU usage of the Deployment's Pods from the metrics API. metrics-server collects it from each node's kubelet about once a minute, which is why `kubectl top` and the HPA lag behind reality. Then:

```text
desired replicas = ceil( current replicas × current utilization / target utilization )
utilization      = average CPU usage of the Pods / CPU request
```

Example from the test below: 2 Pods at 212% with a target of 70% gives `ceil(2 × 212 / 70) = 7`, capped at `maxReplicas` 5.

Details that matter:

- **Tolerance.** Nothing happens while the ratio is within 10% of 1.0 (63% to 77% here). That prevents constant small changes.
- **Pods that aren't Ready yet** are left out of the average on the way up, so a starting Pod with no load doesn't talk the HPA out of scaling.
- **Scale-up is fast, scale-down is slow.** Up: no waiting, at most double the Pods (or +4) per 15 seconds. Down: the HPA uses the highest recommendation of the last 300 seconds (`stabilizationWindowSeconds`), then removes one Pod per minute (set in the chart). Removing a Pod too early costs users latency; keeping one too long costs a little CPU.
- **`replicas` is gone from the Deployment** when autoscaling is on. With a number in the manifest, every `helm upgrade` would set the Pod count back to it, also during a peak. The HPA owns that field now.
- **minReplicas is 2**, so a node drain or a crash never leaves zero Pods, and the PodDisruptionBudget (rendered when the minimum is above 1) still has something to protect.
- **Without metrics-server** the HPA can't compute anything (`cpu: <unknown>`). It still enforces minReplicas and maxReplicas.

CPU works as the signal here because `/api/charge` is CPU-bound. A service that mostly waits on a database can be overloaded at 20% CPU; it would scale on requests per second or queue length instead, which needs a metrics adapter (after Week 11).

## Load balancing

Requests reach a Pod in two steps, and they balance differently.

- **Service (kube-proxy)** works on layer 4. When a client opens a TCP connection to the Service's IP, one Ready Pod is picked at random, and every request on that connection goes to the same Pod. It balances *connections*.
- **Ingress (Traefik)** works on layer 7. It reads HTTP, holds its own connections to the Pods and picks a Pod per *request* (round robin).

This matters for scaling. A client with a long-lived keep-alive connection straight to the Service stays on its old Pod, and a new Pod gets nothing from it. That is why `load-test.js` sets `noConnectionReuse`: a new connection per request, so each request can land on any Pod, including the new ones. Behind an Ingress or a service mesh this happens anyway.

A new Pod gets traffic only once its readiness probe passes, and a Pod being removed is taken out of the endpoints before it stops (Week 8's `preStop` sleep and SIGTERM handling). Scaling down is the same sequence as replacing a Pod in a rollout, so the zero-downtime work from Week 8 is what makes removing Pods safe.

## Why each choice was made

- **A real session feature, small.** The app had no sessions, and "move sessions to Redis" needs something to move. Three endpoints and a cookie are the least that shows the problem and the fix. The in-memory mode stays as the fallback, like the other endpoints, and makes the "before" state reproducible with two local processes.
- **Redis hash with expiry, no session library.** Four Redis commands, all visible in the handler. No new dependency.
- **The HPA lives in the chart, off by default.** dev has one Pod and no use for it; staging turns it on. One `if` in the template decides between `replicas:` and the HPA, so the two can't both be set.
- **CPU, `autoscaling/v2`, no custom metrics.** CPU needs only metrics-server, which k3s ships. `v2` is the current API and the one with `behavior`.
- **k6 in a Pod, through the Service.** Nothing to install, the same in CI, and the measurement doesn't include the laptop's tunnel into the VM. The cost: on a one-node cluster the load generator shares CPU with what it tests.
- **One script does load, timeline and verdict.** The success criterion (up under load, back down after) becomes an exit code instead of something to watch for.
- **CI checks sessions, not scaling.** kind has no metrics-server, and a scaling test on a shared 4-core runner would fail for reasons unrelated to the code. CI proves the part that is deterministic: two Pods, k6, every session found.

## Verification

Run on 2026-11-12 on Rancher Desktop (k3s v1.36.2, one node with 4 CPUs, context `rancher-desktop`), Helm 4.2.1, metrics-server 0.8.1, k6 2.3.0, in `urdnot-staging`. The image `urdnot-api:week9-a` was built from this branch.

**Unit tests:** `python -m pytest app/tests -q` → `28 passed` (6 new). They cover the session lifecycle, expiry, two app instances without Redis disagreeing about a session (401), two instances sharing a fake Redis agreeing, and 503 when Redis is down.

**Static checks:** `helm lint --strict` passes for dev and staging. dev renders no HPA and keeps `replicas: 1`; staging renders the HPA and a Deployment without `replicas`.

**Compose smoke test:** `docker compose up -d --build --wait`, then `scripts/smoke-test.sh` → passed, including the new line `ok   session login, read from Redis, logout`.

**Without Redis, a second replica rejects the session.** Two local processes with no `REDIS_URL`; login on port 8001, then the same cookie against both:

```text
8001 200
8002 401
```

**With Redis, any Pod serves it.** Login through the Ingress on staging, then six reads with the same cookie: `visits` counted 1 to 6 while `served_by` alternated between two Pods.

**Turning the autoscaler on** (`scripts/deploy.sh staging week9-a`, with the Week 8 prober running). Six seconds after the upgrade the Deployment had 1 Pod (the `replicas` field was removed, and the default is 1). The HPA event reads `New size: 2; reason: Current number of replicas below Spec.MinReplicas`, and 27 seconds after the upgrade 2 of 2 were Ready. The prober counted 133 requests around the upgrade, 0 failed.

**Load test** (`scripts/load-test.sh urdnot-staging`, defaults: 10 users, 3 minutes, 100 ms CPU per request):

```text
10:15:08  start      cpu    9% of request (target 70%)  pods 2 ready 2  hpa wants 2
10:15:40  load       cpu  108% of request (target 70%)  pods 4 ready 4  hpa wants 4
10:15:51  load       cpu  250% of request (target 70%)  pods 5 ready 4  hpa wants 5
10:16:01  load       cpu  250% of request (target 70%)  pods 5 ready 5  hpa wants 5
10:16:22  load       cpu  169% of request (target 70%)  pods 5 ready 5  hpa wants 5
   …                 (158% to 172% until the load ends)
10:19:02  cool-down  cpu  172% of request (target 70%)  pods 5 ready 5  hpa wants 5
10:19:23  cool-down  cpu    9% of request (target 70%)  pods 5 ready 5  hpa wants 5
10:24:06  cool-down  cpu    4% of request (target 70%)  pods 4 ready 4  hpa wants 4
10:25:09  cool-down  cpu    5% of request (target 70%)  pods 3 ready 3  hpa wants 3
10:26:13  done       cpu    6% of request (target 70%)  pods 2 ready 2  hpa wants 2

requests        8188 (36.3/s)
failed          0.00 %
checks passed   100.00 %
duration        median 102 ms, p95 282 ms, max 727 ms
```

| What | Measured |
| --- | --- |
| Scale-up | 2 → 4 → 5 Pods; the fifth Pod was Ready about 55 seconds after the test started |
| At the peak | 5 Pods at about 170% of their request (roughly 425m each). The load wanted more Pods than `maxReplicas` allows. |
| Scale-down | First Pod removed 5 minutes after the load ended, then one per minute; back at 2 after 7 minutes |
| Requests | 8188, none failed; every session read was answered with 200 and the right name, also by Pods that started mid-test |
| Week 8 prober, running from the deploy to the end of this test | 6298 requests, 0 failed |

The HPA's own record (`kubectl describe hpa`): `New size: 4` and `New size: 5` with `cpu resource utilization (percentage of request) above target`, then `New size: 4`, `3`, `2` with `All metrics below target`.

**The test can fail.** The first full run reported 49.81% failed requests and exit status 1, while the HPA scaled normally. Every session read got 401. The cause was the k6 script, not the app: k6 empties a virtual user's cookie jar after each iteration unless `noCookiesReset` is set. With that option the run above is clean. It is recorded here because it shows that the session check really detects a session that isn't recognised.

**A load that fits** (`VUS=3 scripts/load-test.sh urdnot-staging`). Three users are about 0.8 cores of work. The HPA went 2 → 3 → 4 → 5 Pods within two minutes and then held 5 Pods at 62% to 66%, inside the tolerance around the 70% target, so it stopped changing anything. 3005 requests, 0 failed, p95 107 ms. Compare that with 282 ms in the 10-user run, where the Pods were over their target and no more could be added. Scale-down again started 5 minutes after the load ended and reached 2 Pods after 7 minutes. Over the whole session the Week 8 prober counted 9359 requests, 0 failed.

**Not verified:**
- The changed `k8s` job in CI (HPA present, two Pods, short k6 run). It runs when this branch is pushed. The same k6 command passed on staging: 640 requests, 0 failed.
- Scaling in CI. kind has no metrics-server there, so only the session check runs.
- Behaviour when the node is full (Pods `Pending`). Five Pods reserve 1.25 of 4 cores, so the limit wasn't reached.
- A rolling update during a load peak.
- Sessions surviving a Redis restart. Redis has a volume and writes RDB snapshots, but the newest sessions could be lost between snapshots; not tested.

## What you should be able to explain in an interview

- Stateful vs stateless, with this service as the example: which state exists, where each piece lives, and why the API tier can be scaled but Postgres can't be scaled the same way.
- What breaks with in-memory sessions and two replicas, and three ways to fix it (shared store, sticky sessions, signed tokens) with the trade-off of each.
- Requests vs limits: who uses each number, and what happens above the CPU limit and above the memory limit.
- The HPA formula, what "70% CPU" is a percentage of, and why a change to the CPU request changes scaling behaviour.
- Why scale-down waits five minutes and scale-up doesn't.
- Why the Deployment has no `replicas` field when an HPA manages it.
- Why new Pods may get no traffic from clients with keep-alive connections, and the difference between layer 4 and layer 7 load balancing.
- What the HPA can't do: it doesn't add nodes (Cluster Autoscaler), doesn't resize Pods (Vertical Pod Autoscaler), and doesn't help when the bottleneck is the database.

## Test your understanding

1. Run `scripts/load-test.sh` and, in a second terminal, `kubectl -n urdnot-staging get hpa urdnot-api -w`. At the first scale-up, put the numbers you see into the formula. Do you get the HPA's answer?
2. Run it with `VUS=2`. Predict the number of Pods first: one user causes roughly 0.27 cores of load (three users needed 5 Pods at 64%).
3. Set `api.resources.requests.cpu` to `500m` with `--set` and repeat the `VUS=2` run. Why does the same load now need fewer Pods? What did it cost?
4. Follow the runbook's "See what breaks without Redis". Then explain why the same test passes in the cluster.
5. During a load test, delete one API Pod (`kubectl delete pod`). Does k6 report failed session checks? Why not?
6. Log in with curl, then `kubectl -n urdnot-staging rollout restart deployment/urdnot-api`. Is the session still valid afterwards? What would the answer be with in-memory sessions?
7. Remove `noConnectionReuse` from `load-test.js` and run the test. Compare `kubectl top pods` across old and new Pods during the hold phase. What do you see, and why?
8. Set `maxReplicas` to 20 and run with `VUS=30`. What state do the extra Pods end up in, and which number on the node ran out?
9. Stop Redis (`kubectl -n urdnot-staging scale statefulset redis --replicas=0`) and call `/api/session` and `/readyz`. What happens to the Pods' readiness, and so to all traffic? Is that the behaviour you want? Scale Redis back to 1.

## Assumptions and open items

- **The default load saturates `maxReplicas`.** By the formula, ten users want `ceil(5 × 170 / 70) = 13` Pods; the HPA stops at 5 and they run at 170% of their request. That shows the cap, but latency at the peak (p95 282 ms for a 100 ms request) is the price. A production setup would raise `maxReplicas`, or the request, until the peak fits.
- **One node.** The load generator, the API, Postgres and Redis share 4 CPUs, so latency numbers describe this laptop, not the service. More Pods on one node also add no failure tolerance; `topologySpreadConstraints` only matter with several nodes.
- **The HPA adds Pods, not nodes.** When requests no longer fit, Pods stay `Pending`. Adding nodes is the Cluster Autoscaler's job and needs a cloud provider (Week 10 onwards).
- **CPU is the only signal.** It fits `/api/charge`. Scaling on requests per second or latency needs Prometheus and an adapter (after Week 11).
- **The first deploy that enables autoscaling dips to 1 Pod** for about 20 seconds. No request was lost here. On a busy service, set `replicas` to the current count once, or apply the HPA first and remove the field in a second step.
- **Sessions have no login.** Anyone can create a session under any name. The feature exists to show where session state lives; it protects nothing. `POST /api/hollows` still uses the clan secret.
- **The cookie has no `Secure` flag**, because the labs use plain HTTP. Add it together with TLS.
- **Redis is now a single point of failure for logged-in users.** One replica, no password, and `/readyz` fails for every API Pod when it is down. A managed Redis or a replicated setup is the production answer; it is out of scope here.
- **`requests` for Postgres and Redis were not revisited.** Only the API's CPU request changed.
- **CI doesn't test scaling**, only that sessions work on two Pods. A scaling test needs metrics-server in kind and a runner with predictable CPU.
- **The Kustomize weeks (5 and 6) have no HPA.** They are kept as the record of those weeks and still render in CI.
- **Open items from Week 8 that still stand:** the Postgres and Redis image pins live in three places, the chart isn't published from CI, and there is no migration tool.
