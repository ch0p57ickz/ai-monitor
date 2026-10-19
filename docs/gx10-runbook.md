# GX10 runbook: ai-monitor usage service

Commands for starting, upgrading, backing up, rolling back, and rotating
credentials for the loopback usage service on the GX10 host (`zoey`).

**Verification status:** the start/stop/backup/rollback commands below are
standard `systemd` and `sqlite3` invocations, and each path is pinned to the
layout used by `deploy/ai-monitor.service`. They have **not** yet been executed
against a deployed unit on this host: tasks 5.2 (install under a dedicated
account), 5.3 (Tailscale Serve), and the soak in 5.1 are still pending operator
approval. Treat the steps as reviewed-but-unexecuted until the first install.

## Layout

| Path | Purpose |
| --- | --- |
| `/opt/ai-monitor` | Code checkout and virtualenv (`venv/`) |
| `/etc/ai-monitor/config.json` | Configuration, mode `0600`, owner `ai-monitor` |
| `/var/lib/ai-monitor/usage.db` | SQLite database (WAL), persistent state |
| `/home/ai-monitor` | Service account home (Codex socket, Antigravity CLI) |
| `deploy/ai-monitor.service` | Unit file in this repository |

## Verify the service locally (no install)

```sh
# From the repository root. Starts the service on a loopback port with a
# temporary data directory, uses the real adapters, and exercises the HTTP
# contract. Exits non-zero if a check fails.
python3 tools/smoke_check.py
```

The smoke check asserts: `/health` lists all four source IDs, `/api/v1/usage`
returns `observations: []` before any source succeeds, history reports `hours`
and `truncated`, every response is `Cache-Control: private, no-store`, no
response contains credential-looking text, responses stay fast while providers
fail, and a non-loopback address cannot reach the port.

## Soak the service locally (task 5.1 evidence)

```sh
# Runs the real service and real adapters for a bounded interval, sampling the
# loopback API throughout. Exits non-zero if an expectation fails; writes only
# to a temporary data directory.
python3 tools/soak_check.py --seconds 150
```

It reports response-latency percentiles, per-source status transitions, and
observation/`truncated` counts, then asserts: at least one source reaches
healthy, every response stays well under the per-attempt deadline, exactly one
committed observation is exposed once a source first succeeds, and failing
sources coexist independently. Results are recorded in
`docs/gx10-capability-report.md`.

```sh
# Collects Codex usage through the real adapter (spawns codex itself).
python3 tools/codex_live_check.py
```

## Verify both OpenRouter streams (task 5.1 evidence)

The two OpenRouter streams use different endpoints and different credential
permissions, so one can collect while the other fails — that partial result is
what this check verifies. It reads credentials from the environment only and
never prints them, so export them in the shell rather than passing them as
arguments:

```sh
set -a; . ./.env; set +a
PYTHONPATH=. python3 tools/openrouter_live_check.py
```

It reports each stream's status, its error category, and its budget fields, then
asserts that at least one stream collected and that no credential appeared in an
observation payload. Exit codes: `0` collected, `2` no credential present (a
configuration problem), `1` anything else. Without a management key the
account-credit stream falls back to the inference key and may report
`authorization` while the current-key stream succeeds.

## Watch a quota window across a reset (task 5.1 evidence)

A reset is where the pace math is easiest to get wrong: the comparison target
has to drop back to the *new* window's elapsed fraction rather than stay at the
elapsed fraction of the window that just ended. Find the current reset time
first (`/api/v1/usage` reports it as `resetsAt` on each window), then watch
across it:

```sh
# Runs the real service with the real adapters, samples /api/v1/usage once a
# minute, records every sample as JSON Lines, and checks what happened at the
# reset. --require-reset makes the run fail unless a window turnover is spanned;
# drop it for a short watch that only checks the drift rule. Detach it if the
# watch is longer than your session.
python3 tools/reset_watch.py --minutes 50 --require-reset --log /tmp/opencode/reset-watch.jsonl
```

It asserts that a reset was observed, that the window id and duration survive
it, that a window whose end drifts with the clock is reported as `unknown`
rather than classified, and that history stayed bounded and the API never
returned a non-200. Right after a reset, pace must be either `unknown` (a
provisional boundary, the honest answer) or classified against a *small*
expectation — never against the window that just ended, which would read ~100.

The rules can be re-applied to a recorded log, which is how the drift guard
demonstrates that it bites without waiting for another turnover:

```sh
# Replaying the pre-fix log fails, naming each fabricated label; replaying the
# post-fix log passes.
python3 tools/reset_watch.py --replay /tmp/opencode/reset-watch.jsonl --require-reset
```

The JSON Lines log is the raw evidence; results are recorded in
`docs/gx10-capability-report.md`.

## Install (task 5.2, first time)

```sh
# 1. Dedicated unprivileged account with its own home.
sudo useradd --system --create-home --home-dir /home/ai-monitor --shell /usr/sbin/nologin ai-monitor

# 2. Code and virtualenv, owned by the service account.
sudo install -d -o ai-monitor -g ai-monitor /opt/ai-monitor
sudo -u ai-monitor git clone <repo-url> /opt/ai-monitor
sudo -u ai-monitor python3 -m venv /opt/ai-monitor/venv
sudo -u ai-monitor /opt/ai-monitor/venv/bin/pip install /opt/ai-monitor

# 3. Configuration with credentials, readable only by the service account.
sudo install -d -m 0750 -o root -g ai-monitor /etc/ai-monitor
sudo install -m 0640 -o root -g ai-monitor deploy/config.example.json /etc/ai-monitor/config.json
sudoedit /etc/ai-monitor/config.json   # set codex_bin, the agy path, keys

# Generate the account digest key used to derive opaque account ids. Without
# it, account-scoped trends (same-account pace, spend deltas) are disabled and
# the service logs a warning at startup. It must be at least 16 characters.
python3 -c 'import secrets; print(secrets.token_hex(32))'
# Then set "account_digest_key" to that value in the config.

# 4. Unit, then start.
sudo install -m 0644 deploy/ai-monitor.service /etc/systemd/system/ai-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-monitor
```

Give the service account its own provider sign-in before expecting data:

```sh
# Codex, default (stdio): nothing to prepare. The service starts
# `codex app-server --listen stdio://` itself and speaks the protocol over the
# child's pipes, which needs no daemon and no host policy change. The rate-limit
# read runs no sandboxed tools, so the AppArmor restriction described below does
# not apply. `codex_bin` must resolve for the service account.

# Codex, alternative (socket): start the app-server as the service account so
# the control socket exists, set "codex_transport": "socket", then confirm the
# socket actually answers.
sudo -u ai-monitor codex app-server daemon start
sudo -u ai-monitor ls -l /home/ai-monitor/.codex/app-server-control/
# `codex app-server proxy` failing with a broken pipe means the daemon accepts
# and then closes every connection. See the AppArmor note below.

# Verify either transport with the real adapter (spawns codex itself):
python3 tools/codex_live_check.py

# On Ubuntu 24.04 the usual cause of a dead daemon is the AppArmor
# user-namespace restriction:
#   sysctl kernel.apparmor_restrict_unprivileged_userns   -> 1
#   bwrap --ro-bind / / --dev /dev true   -> "setting up uid map: Permission denied"
# Only the socket transport needs the daemon, so the simplest fix is to leave the
# hardening in place and use the default stdio transport. If you do need the
# daemon, grant the codex binary a user-namespace AppArmor profile, or relax the
# restriction system-wide (weakens host hardening):
#   sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0

# Antigravity: the installed variant on this host has no usage subcommand, so
# `antigravity` is expected to report `unavailable` (see the capability report).
```

## Health and logs

```sh
systemctl status ai-monitor --no-pager
journalctl -u ai-monitor -n 100 --no-pager
curl -s http://127.0.0.1:8732/health | python3 -m json.tool
curl -s http://127.0.0.1:8732/api/v1/version
```

The log filter redacts credentials, tokens, email addresses, and opaque
session strings. A credential must never appear in `journalctl` output or in a
response body:

```sh
journalctl -u ai-monitor --no-pager | grep -Ei 'bearer|api[_-]?key|sk-|@'
# Expect no matches.
```

## Upgrade

```sh
sudo -u ai-monitor git -C /opt/ai-monitor fetch --tags
sudo -u ai-monitor git -C /opt/ai-monitor checkout <tag-or-sha>
sudo -u ai-monitor /opt/ai-monitor/venv/bin/pip install /opt/ai-monitor
sudo systemctl restart ai-monitor
systemctl status ai-monitor --no-pager
```

Schema changes are applied on open; take a backup first (below). A failed
upgrade is reverted by checking out the previous revision.

## Backup

The only stateful artifact is the SQLite database. Use the online backup so a
running service stays consistent:

```sh
sudo -u ai-monitor sqlite3 /var/lib/ai-monitor/usage.db ".backup '/var/lib/ai-monitor/backup-$(date +%F).db'"
sudo install -m 0600 -o ai-monitor -g ai-monitor \
  /var/lib/ai-monitor/backup-$(date +%F).db /var/lib/ai-monitor/backups/
```

Also back up `/etc/ai-monitor/config.json` to a restricted location (it holds
credentials) and `deploy/ai-monitor.service`. Never store the config in a
world-readable directory or a git remote.

Restore:

```sh
sudo systemctl stop ai-monitor
sudo -u ai-monitor sqlite3 /var/lib/ai-monitor/usage.db ".restore '/var/lib/ai-monitor/backups/backup-YYYY-MM-DD.db'"
sudo systemctl start ai-monitor
```

## Rollback

```sh
sudo -u ai-monitor git -C /opt/ai-monitor checkout <previous-good-sha>
sudo -u ai-monitor /opt/ai-monitor/venv/bin/pip install /opt/ai-monitor
sudo systemctl restart ai-monitor
```

If the database schema moved forward, restore the matching backup as well.

## Credential rotation

```sh
sudoedit /etc/ai-monitor/config.json     # replace openrouter_api_key / management key
sudo systemctl restart ai-monitor
curl -s http://127.0.0.1:8732/health | python3 -m json.tool
```

`openrouter-account` uses `openrouter_management_key` (falling back to
`openrouter_api_key`); `openrouter-key` always uses `openrouter_api_key`. A
non-management key calling `/api/v1/credits` returns HTTP 403, which the
service reports as `operator_action` / `authorization` for the account stream
while the key stream keeps working.

After rotating, confirm the config file mode is still `0640` (or stricter) and
that no new secret appears in the journal:

```sh
sudo stat -c '%a %U:%G' /etc/ai-monitor/config.json
journalctl -u ai-monitor --since '5 min ago' --no-pager | grep -Ei 'bearer|sk-|@' || true
```

## Tailscale Serve (task 5.3, pending)

The service binds `127.0.0.1` only. Publication is via Tailscale Serve, never
Funnel:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8732
tailscale serve status
```

Then restrict access to the intended tailnet identity and devices and confirm
that a disallowed device is denied, and that `tailscale funnel status` shows
Funnel disabled. This requires the tailnet access rules and the Mac/iPhone
devices, so it is executed with the operator present.
