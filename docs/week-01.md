# Week 1: Linux, systemd, Nginx, SSH, UFW, and DNS

Week 1 put `urdnot-api` on a plain Ubuntu 24.04 VM the traditional way, before any containers. The configuration lives in [`infra/week1/`](../infra/week1/), and the operator runbook is [`infra/week1/README.md`](../infra/week1/README.md). This document explains the decisions behind those files.

> **Sources.** This write-up is based on the committed files in `infra/week1/` and `app/`, and on the git history (`dfa6077`, `98897af`). The Codex working notes (`week1-ledger.md` and `ledger.md`) were not available when this was written. Where this document and those notes disagree, the repository is authoritative. See [Discrepancies and open items](#discrepancies-and-open-items).

## What was built

The lab ran in an iximiuz Labs Ubuntu 24.04 playground and was verified on 2026-09-21 against commit `3fe6c27`. All 17 application tests passed.

| Piece | File | What it does |
| --- | --- | --- |
| App service | `urdnot.service` | systemd runs `python -m app` from a venv as the unprivileged `urdnot` user. It restarts the app on failure and gives it 30 s to drain on stop. |
| Reverse proxy | `urdnot.nginx.conf` | Nginx on port 80 is the `default_server`. It proxies to `127.0.0.1:8000`, passes `X-Forwarded-*` and `X-Real-IP`, and keeps its own access and error logs. |
| Health probe | `urdnot-healthcheck` | A Bash script (`set -euo pipefail`) curls `/healthz`, then `/readyz`, through Nginx and exits 1 on the first failure. |
| Probe scheduling | `urdnot-healthcheck.service` and `.timer` | A oneshot unit, sandboxed and run as `urdnot`. The timer fires it 60 s after boot and then every 60 s. Results go to the journal. |
| Log summary | `urdnot-log-summary` | An `awk` script that counts HTTP status codes in the Nginx `combined` access log. |
| DNS | `urdnot-dns.conf` and `urdnot-dns.service` | `dnsmasq` listens only on `127.0.0.2:53` and answers for `urdnot.test`. The system resolver is not changed. |
| SSH hardening | `90-week1ops.conf` | A `Match User` block allows only public-key authentication for a practice account. It also disables forwarding and TTY allocation. |
| Firewall | Documented only | UFW denies inbound by default and allows outbound. SSH (22) and HTTP (80) are open, plus the playground's two management ports from `172.16.0.0/24`. |

## Why each choice was made

- **Run the app as a dedicated system user instead of root.** A compromise of the app then does not own the box. `NoNewPrivileges=true` blocks privilege gain through setuid binaries, and `PrivateTmp=true` isolates `/tmp`.
- **Bind the app to `127.0.0.1` and put Nginx in front.** Only Nginx is reachable from the network. It handles slow clients, access logging, and a future TLS setup, and Uvicorn is never exposed directly.
- **Keep secrets in `EnvironmentFile=/etc/urdnot.env` (root-owned, mode 600).** Secrets stay out of the unit file and out of git. The default `CLAN_SECRET=shiagur` is public in the README, so every deployment must override it.
- **Set `Restart=on-failure` and `RestartSec=3`.** systemd recovers from crashes without looping, and a clean exit (status 0 after SIGTERM) is not treated as a failure.
- **Set `TimeoutStopSec=30`.** On SIGTERM, the entry point drains in-flight requests. `/api/charge` can run for up to 10 s, so 30 s leaves headroom before systemd sends SIGKILL.
- **Use a systemd timer instead of cron for the health check.** The timer gives journal logging, `systemctl list-timers` visibility, the `OnBootSec` delay, and the same sandboxing directives as any unit.
- **Check liveness and readiness separately.** `/healthz` means the process serves requests. `/readyz` means its configured dependencies work. The script checks both, through Nginx, so it tests what a user would hit.
- **Use curl flags `--fail --connect-timeout 2 --max-time 5`.** HTTP errors become non-zero exits, and a hung backend cannot stall the timer.
- **Run `dnsmasq` on `127.0.0.2` with `bind-interfaces`.** This practices authoritative DNS without taking over the VM's real resolver, which is the fastest way to break a remote box.
- **Scope the SSH restriction to one account with `Match User`.** The platform login stays untouched. Locking yourself out of a remote VM is the classic SSH mistake.
- **Allow UFW management ports only from the playground subnet, and arrange a rollback first.** Enabling a firewall remotely without a timed rollback is how you lose access.

## Verification evidence

The evidence comes from `infra/week1/README.md`. It was captured in the playground and is not reproducible from this repository alone.

- `systemd-analyze verify`, `nginx -t`, and `sshd -t` are the pre-flight checks before any reload.
- The timer logged success every minute. It failed with exit 1 during a deliberate backend outage and succeeded again after a restart.
- DNS answered the A record over UDP and TCP, and returned NXDOMAIN for `missing.test`.
- UFW was tested with a temporary network namespace. Port 80 was reachable and an unapproved listener on 8099 was blocked. A temporary allow rule made 8099 reachable, and removing it blocked the port again.
- All units, Nginx, and UFW came back after a VM reboot.

## How to build and run it

The step-by-step install is in [`infra/week1/README.md`](../infra/week1/README.md#install-and-verify-the-services). In short: clone to `/opt/urdnot`, create `app/.venv`, and write `/etc/urdnot.env`. Then copy the files to their install paths, run the three verify commands, and `systemctl enable --now` the units. `urdnot-dns.conf` contains an example VM address, `172.16.0.2`, which must be edited first.

## What you should be able to explain in an interview

- The request path, `client → :80 Nginx → 127.0.0.1:8000 Uvicorn → FastAPI`, and why the app is not bound to `0.0.0.0`.
- The difference between liveness and readiness, and why a Postgres outage should fail readiness but not liveness.
- What happens during a SIGTERM: stop accepting connections, drain requests, exit 0. How `TimeoutStopSec` bounds that, and why exit 0 matters with `Restart=on-failure`.
- Why a systemd timer beats cron here, and why a oneshot unit shows as "inactive" between runs.
- How you would enable a firewall on a remote machine without locking yourself out.
- Why secrets go in a mode-600 environment file and not in the unit file or the repository.

## Test your understanding

1. The health check script calls `http://127.0.0.1` (Nginx), not `:8000`. What class of failure does that catch that a direct check would miss, and what does it miss in return?
2. You deploy with `Restart=always` instead of `on-failure`, and an operator runs `systemctl stop urdnot`. What happens, and why?
3. `/etc/urdnot.env` is mode 644 on one host. Who can read `CLAN_SECRET` there, and why doesn't mode 600 stop the `urdnot` service from reading it?

## Discrepancies and open items

- **Codex ledgers not reconciled.** This document was written in a remote build environment that could not reach `~/Documents/Codex/2026-09-21/e/work/urdnot/`. Before treating it as final, check both ledgers against the "Verification evidence" section, especially the test count, management ports, and reboot test. Record any differences here. Where a ledger disagrees with a committed file, the committed file wins.
- **The config moved after verification.** The lab was verified against `3fe6c27`, but the files were committed in `dfa6077` under `app/ops/week1/` and moved to `infra/week1/` in `98897af`. The move was a pure rename with no content change, so the verification still applies.
- **UFW is documented, not captured as code.** No rules file or script exists, so the firewall state can't be reproduced from the repository.
- **Hardening is uneven.** `urdnot-healthcheck.service` uses `ProtectSystem=strict` and `ProtectHome=true`, but the long-running `urdnot.service` does not, although it is the larger attack surface. This could be a future hardening pass. The Week 1 files were intentionally left unchanged.
- **The health check has no alerting.** Failures only reach the journal. Monitoring arrives later in the curriculum.
