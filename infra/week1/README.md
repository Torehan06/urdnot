# Week 1: Ubuntu operations lab

These are the configuration files and Bash utilities used to run Urdnot in an iximiuz Labs Ubuntu 24.04 playground. The application code stays under `app/`; this directory records the Linux, Nginx, SSH, DNS, and monitoring exercise. The lab was verified on September 21, 2026 against commit `3fe6c270ee9f921cb178987809093afa7cbcb1cc` with 17 passing application tests.

## Layout

| Repository file | Install path | Purpose |
| --- | --- | --- |
| `urdnot.service` | `/etc/systemd/system/urdnot.service` | Run the API as an unprivileged user |
| `urdnot.nginx.conf` | `/etc/nginx/sites-available/urdnot` | Proxy port 80 to `127.0.0.1:8000` |
| `urdnot-healthcheck` | `/usr/local/bin/urdnot-healthcheck` | Exit nonzero if health or readiness fails |
| `urdnot-log-summary` | `/usr/local/bin/urdnot-log-summary` | Count statuses in the current Nginx access log |
| `urdnot-healthcheck.service` and `.timer` | `/etc/systemd/system/` | Run the check about once a minute |
| `urdnot-dns.conf` and `urdnot-dns.service` | `/etc/urdnot-dns.conf` and `/etc/systemd/system/` | Serve an isolated `urdnot.test` DNS answer |
| `90-week1ops.conf` | `/etc/ssh/sshd_config.d/` | Restrict the separate SSH practice account |

The files assume the repository is cloned to `/opt/urdnot`, Python 3.12 dependencies are installed in `/opt/urdnot/app/.venv`, and an unprivileged `urdnot` system user owns the checkout. The VM needs `nginx`, `curl`, `dnsmasq-base`, `ufw`, and `openssh-server`. `shellcheck` and `dnsutils` are useful for validation. Set the service's `HOST=127.0.0.1`, `PORT=8000`, empty `DATABASE_URL`/`REDIS_URL`, and `CLAN_SECRET` in a root-owned `/etc/urdnot.env` with mode `600`. The default `shiagur` is public because it appears in the root README, and it is only for local development. Every deployed instance must set its own value, for example with `openssl rand -hex 16`; store it only in the mode-600 root-owned environment file and never commit it. Do not commit that environment file or any SSH private key.

`urdnot-dns.conf` contains the **example VM address** `172.16.0.2`. Replace it with the current VM address before starting the DNS unit. Its listener is `127.0.0.2` only; the VM's normal DNS resolver is unchanged. `urdnot.test` is a local test name, not a public record.

The SSH example uses a separate `week1ops` account and a public key in `/home/week1ops/.ssh/authorized_keys` prefixed with `restrict,from="127.0.0.1"`. The practice keys lived only inside the VM. `90-week1ops.conf` requires public-key authentication for that account and disables forwarding and interactive TTY. Keep the account's `.ssh` directory at mode `700` and `authorized_keys` at `600`. Test a new key before removing the old one. This example intentionally does not change the platform's existing login account.

## Install and verify the services

After copying the files to the paths above, enable their units and Nginx site:

```bash
sudo ln -s /etc/nginx/sites-available/urdnot /etc/nginx/sites-enabled/urdnot
sudo chmod 755 /usr/local/bin/urdnot-healthcheck /usr/local/bin/urdnot-log-summary
sudo systemd-analyze verify /etc/systemd/system/urdnot.service /etc/systemd/system/urdnot-dns.service /etc/systemd/system/urdnot-healthcheck.service /etc/systemd/system/urdnot-healthcheck.timer
sudo nginx -t
sudo sshd -t
sudo systemctl daemon-reload
sudo systemctl enable --now urdnot nginx urdnot-dns urdnot-healthcheck.timer
urdnot-healthcheck
sudo urdnot-log-summary
dig @127.0.0.2 urdnot.test A +short
```

On a fresh Ubuntu image, remove the default Nginx site symlink before enabling this `default_server` site. Inspect `/etc/nginx/sites-enabled` first if the machine already hosts other sites. `urdnot-healthcheck.service` is a oneshot unit: it is normally inactive between timer runs. Check its journal for results.

## Firewall exercise

The lab used UFW with deny incoming and allow outgoing. It allowed SSH (22/tcp) and HTTP (80/tcp). The observed iximiuz management listeners were 50061/tcp and 40059/tcp, allowed only from `172.16.0.0/24` in that playground. **Inspect the current playground's listeners and network first; those management ports and source range are environment specific.** Arrange an automatic rollback before enabling UFW remotely, and verify a fresh SSH or platform shell session plus the browser endpoint before cancelling rollback.

```bash
sudo ufw status verbose
sudo ss -lntp
sudo journalctl -k --no-pager | grep 'UFW BLOCK'
```

A controlled test used a temporary network namespace to verify port 80 was reachable, an unapproved listener on 8099 was blocked, a temporary allow made 8099 reachable, and removing that allow blocked it again. The temporary rule and namespace were removed afterward.

## DNS and observability checks

```bash
dig github.com A +noall +answer
dig @127.0.0.2 urdnot.test A +noall +answer
dig @127.0.0.2 urdnot.test A +tcp +noall +answer
dig @127.0.0.2 missing.test A +noall +comments
systemctl list-timers urdnot-healthcheck.timer
sudo journalctl -u urdnot-healthcheck -n 20 --no-pager
```

The DNS service returned the local A record over UDP and TCP and NXDOMAIN for an unknown name. The timer logged success each minute, failed with exit status 1 during a controlled backend outage, and succeeded again after Urdnot restarted. It writes to the journal; it does not send alerts.

The playground exposed **Nginx port 80** through a private iximiuz HTTPS URL. Urdnot stayed bound to loopback. Both services, the DNS unit, the timer, and UFW recovered after a VM reboot. The playground is persistent but stops at expiry; its in-memory application data resets when the process restarts.
