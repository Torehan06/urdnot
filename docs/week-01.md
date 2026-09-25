# Week 1: Linux, systemd, Nginx, SSH, UFW, and DNS

Week 1 put `urdnot-api` on a plain Ubuntu 24.04 VM the traditional way, before any containers. The configuration lives in [`infra/week1/`](../infra/week1/), and the operator runbook is [`infra/week1/README.md`](../infra/week1/README.md). This document explains the decisions behind those files.

> **Sources.** This write-up is based on the committed files in `infra/week1/` and `app/`, and on the git history (`dfa6077`, `98897af`). On 2026-09-25 it was reconciled against the Codex working notes from the lab session (`ledger.md`, `week1-ledger.md`, and a `files.json` config snapshot). Those notes are private and not part of this repository. Where they disagree with a committed file, the repository is authoritative. See [Discrepancies and open items](#discrepancies-and-open-items).

## What was built

The lab ran in an iximiuz Labs Ubuntu 24.04.4 playground (Python 3.12.3, Nginx 1.24.0, systemd as PID 1). It was verified on 2026-09-21 against commit `3fe6c27`: `pip check` passed and all 17 application tests passed, with 2 upstream deprecation warnings. No application code was changed during the lab. The configuration was committed afterward, on 2026-09-23.

| Piece | File | What it does |
| --- | --- | --- |
| App service | `urdnot.service` | systemd runs `python -m app` from a venv as the unprivileged `urdnot` user. It restarts the app on failure and gives it 30 s to drain on stop. |
| Reverse proxy | `urdnot.nginx.conf` | Nginx on port 80 is the `default_server`. It proxies to `127.0.0.1:8000`, passes `X-Forwarded-*` and `X-Real-IP`, and keeps its own access and error logs. |
| Health probe | `urdnot-healthcheck` | A Bash script (`set -euo pipefail`) curls `/healthz`, then `/readyz`, through Nginx and exits 1 on the first failure. |
| Probe scheduling | `urdnot-healthcheck.service` and `.timer` | A oneshot unit, sandboxed and run as `urdnot`. The timer fires it 60 s after boot and then every 60 s. Results go to the journal. |
| Log summary | `urdnot-log-summary` | An `awk` script that counts HTTP status codes in the Nginx `combined` access log. |
| DNS | `urdnot-dns.conf` and `urdnot-dns.service` | `dnsmasq` listens only on `127.0.0.2:53` and answers for `urdnot.test`. The system resolver is not changed. |
| SSH hardening | `90-week1ops.conf` | A `Match User` block allows only public-key authentication for a practice account (`week1ops`). It also disables forwarding and TTY allocation. The account's Ed25519 key is also restricted in `authorized_keys` with `restrict,from="127.0.0.1"` (documented in the runbook, not committed). |
| Firewall | Documented only | UFW denies inbound by default and allows outbound. SSH (22) and HTTP (80) are open, plus the playground's two management ports from `172.16.0.0/24`. |

## Why each choice was made

- **Run the app as a dedicated system user instead of root.** A compromise of the app then does not own the box. `NoNewPrivileges=true` blocks privilege gain through setuid binaries, and `PrivateTmp=true` isolates `/tmp`.
- **Bind the app to `127.0.0.1` and put Nginx in front.** Only Nginx is reachable from the network. It handles slow clients, access logging, and a future TLS setup, and Uvicorn is never exposed directly.
- **Keep secrets in `EnvironmentFile=/etc/urdnot.env` (root-owned, mode 600).** Secrets stay out of the unit file and out of git. The default `CLAN_SECRET=shiagur` is public in the README, so every deployment must override it. systemd reads the file only when the unit starts, so editing it has no effect until `systemctl restart urdnot`. This was verified in the lab. Rotating the secret is therefore a restart, not just a file edit.
- **Set `Restart=on-failure` and `RestartSec=3`.** systemd recovers from crashes without looping, and a clean exit (status 0 after SIGTERM) is not treated as a failure.
- **Set `TimeoutStopSec=30`.** On SIGTERM, the entry point drains in-flight requests. `/api/charge` can run for up to 10 s, so 30 s leaves headroom before systemd sends SIGKILL.
- **Use a systemd timer instead of cron for the health check.** The timer gives journal logging, `systemctl list-timers` visibility, the `OnBootSec` delay, and the same sandboxing directives as any unit. `AccuracySec=1s` matters: the default is one minute, which lets systemd shift a 60 s timer by up to a minute to batch wakeups. The lab logged runs at 12:04:31 and 12:05:32 UTC.
- **Check liveness and readiness separately.** `/healthz` means the process serves requests. `/readyz` means its configured dependencies work. The script checks both, through Nginx, so it tests what a user would hit.
- **Use curl flags `--fail --connect-timeout 2 --max-time 5`.** HTTP errors become non-zero exits, and a hung backend cannot stall the timer.
- **Run `dnsmasq` on `127.0.0.2` with `bind-interfaces`.** This practices authoritative DNS without taking over the VM's real resolver, which is the fastest way to break a remote box. Public resolution of `github.com` was checked afterward to prove the system resolver still worked.
- **Scope the SSH restriction to one account with `Match User`.** The platform login stays untouched. Locking yourself out of a remote VM is the classic SSH mistake.
- **Restrict the practice key twice.** `restrict,from="127.0.0.1"` in `authorized_keys` limits where the key works and what it can do, even if the sshd drop-in is removed. The sshd `Match` block enforces the same limits from the server side. Keys were generated and kept inside the VM, and the client used strict host-key checking against the VM's own host key, so no private key ever left the lab.
- **Rotate keys by adding the new one before removing the old one.** The lab tested the full lifecycle: an unknown key was rejected, the installed key was accepted, a replacement key was accepted, and the revoked key was then rejected.
- **Allow UFW management ports only from the playground subnet, and arrange a rollback first.** Enabling a firewall remotely without a timed rollback is how you lose access. The rollback was cancelled only after a *fresh* remote command and a browser load succeeded. An already-open session proves nothing, because established connections survive the rule change.
- **Test the firewall from a separate network namespace.** A temporary namespace with a veth pair acts as an outside client on the same VM. It exercises the real UFW rules without exposing test ports to the internet, and it can be deleted cleanly afterward.

## Verification evidence

The evidence comes from `infra/week1/README.md` and the Codex lab notes. It was captured in the playground, which was scheduled to expire around 19:48 UTC on 2026-09-21, so it cannot be reproduced from this repository alone.

- **Pre-flight.** `systemd-analyze verify`, `nginx -t`, and `sshd -t` run before any reload. Both Bash utilities passed `shellcheck` and `bash -n`.
- **App and proxy.** The app ran as the non-root `urdnot` user with a root-owned mode-600 environment file. It listened on `127.0.0.1:8000`, and Nginx listened on all interfaces on port 80. A SIGTERM to the service produced exit 0. With the backend stopped, Nginx returned a controlled 502, and the check recovered when the backend returned. `urdnot-log-summary` reported the expected counts for known traffic.
- **Timer.** The timer ran every minute (12:04:31 and 12:05:32 UTC). It failed with exit 1 during a deliberate backend outage and succeeded again after a restart.
- **DNS.** The A record for `urdnot.test` (`172.16.0.2`) resolved over UDP and TCP, and `missing.test` returned NXDOMAIN. Public `github.com` still resolved through the unchanged system resolver. HTTP routing by `Host` header was checked against the returned address.
- **SSH.** An unknown key was rejected, the installed key was accepted, a replacement key was accepted, and the revoked key was then rejected.
- **UFW.** UFW was tested with a temporary network namespace. Port 80 was reachable and an unapproved listener on 8099 was blocked. A temporary allow rule made 8099 reachable, and removing it blocked the port again. Kernel logs showed the matching `UFW BLOCK` entries. The temporary rule, namespace, veth pair, and listener were removed afterward.
- **Reboot.** The reboot was proven by a changed `/proc/sys/kernel/random/boot_id` compared with a saved value, not just by uptime. After it, all units, Nginx, DNS, the SSH key, and UFW were active and enabled. The first automatic health check after boot (12:07:47 UTC) passed both `/healthz` and `/readyz`.
- **Config fidelity.** The notes record that SHA-256 hashes of the exported configs matched the live VM files. The Codex `files.json` snapshot includes four of those files (`urdnot.service`, the Nginx site, and the two scripts). On 2026-09-25 they were compared with `infra/week1/` and are byte-for-byte identical, with matching file modes (644/644/755/755).
- **Browser.** The Nginx-served homepage rendered in Chrome through the playground's HTTPS URL, before and after the reboot. The browser client blocked the `/healthz` navigation, although Nginx logged that request as 200. `/readyz` was never checked from a browser. Health and readiness were verified from inside the VM, not end to end from a browser.
- **Review.** An independent read-only review found no blocking configuration defects. One review pass was cut short by a usage limit after its main findings were in, and those findings were incorporated.

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

**Ledger reconciliation (2026-09-25).** The two Codex ledgers and `files.json` were compared with this document and `infra/week1/`.

- **No factual conflicts.** The test count (17), the verified commit (`3fe6c27`), the reboot result, the DNS answers, and the timer behavior all agree. The ledgers only say "observed platform management ports" and never give numbers. The runbook's `50061/tcp` and `40059/tcp` from `172.16.0.0/24` are the authoritative values and should be re-checked in any new playground.
- **Referenced records were never committed.** `ledger.md` says caveats were "incorporated in README and verification.md", and `week1-ledger.md` mentions a "firewall policy table" and a "completion/replay record". There is no `verification.md` or replay record in any commit. The firewall policy exists as prose in the runbook's "Firewall exercise" section, not as a table. Following the repository, this document treats the runbook and this write-up as the record. The evidence above that goes beyond the runbook comes from the ledgers alone.
- **Only part of the config has a snapshot.** `files.json` was written at 14:49, before the SSH, DNS, and timer work. Five committed files (`90-week1ops.conf`, `urdnot-dns.conf`, `urdnot-dns.service`, `urdnot-healthcheck.service`, `urdnot-healthcheck.timer`) have no snapshot. The ledger says their hashes matched but does not record the hashes, so that claim cannot be checked independently.
- **Browser evidence is narrower than the runbook suggests.** The runbook says the playground exposed port 80 through an HTTPS URL, which is true, but only the homepage rendered in a browser. See the Browser item above.
- **Python version differs from Week 2.** The VM ran Ubuntu's Python 3.12.3. The Week 2 container pins 3.12.14. Both are 3.12 and the pins install on both, but they are not the same interpreter build.
- **The config moved after verification.** The lab was verified against `3fe6c27`, but the files were committed in `dfa6077` under `app/ops/week1/` and moved to `infra/week1/` in `98897af`. The move was a pure rename with no content change, so the verification still applies.

**Open items, deferred pending discussion with the internship supervisor:**

- **UFW is documented, not captured as code.** No rules file or script exists, so the firewall state can't be reproduced from the repository.
- **Hardening is uneven.** `urdnot-healthcheck.service` uses `ProtectSystem=strict` and `ProtectHome=true`, but the long-running `urdnot.service` does not, although it is the larger attack surface. This could be a future hardening pass. The Week 1 files were intentionally left unchanged.
- **The health check has no alerting.** Failures only reach the journal. Monitoring arrives later in the curriculum.
