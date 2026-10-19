# GX10 runbook: ai-monitor usage service

Commands for starting, upgrading, backing up, rolling back, and rotating
credentials for the loopback usage service on the GX10 host (`zoey`).

**Verification status:** the backup and restore procedure has been executed end
to end against a live service by `tools/backup_restore_check.py`, and the example
config it installs is loaded by the test suite. Nothing else here has run against
a deployed unit: the install, the `systemctl` lifecycle commands, and the Tailscale
steps need `sudo` and operator approval (tasks 5.2-5.4). The soak and reset checks
in 5.1 were run on this host without privileges. Treat everything outside the
backup/restore procedure as reviewed-but-unexecuted until the first install.

## Layout

| Path | Purpose |
| --- | --- |
| `/opt/ai-monitor` | Code checkout and virtualenv (`venv/`) |
| `/etc/ai-monitor/config.json` | Configuration, mode `0640`, owner `root`, group `ai-monitor` |
| `/var/lib/ai-monitor/usage.db` | SQLite database (WAL), persistent state |
| `/var/lib/ai-monitor/backups/` | Database backups, mode `0700` (created by the backup procedure, not by the unit) |
| `/home/ai-monitor` | Service account home (Codex socket, Antigravity CLI) |
| `deploy/ai-monitor.service` | Unit file in this repository |

## Verify the service locally (no install)

```sh
# From the repository root. Starts the service on a loopback port with a fresh
# temporary data directory, uses the real adapters, and exercises the HTTP
# contract. Exits non-zero if a check fails.
python3 tools/smoke_check.py

# To check a deployment's own settings, point it at that deployment's config.
# Its provider settings and credentials are used, but data_dir and api_port are
# overridden, so the running service and its database are untouched. Reading
# /etc/ai-monitor/config.json (mode 0640) needs root.
sudo python3 tools/smoke_check.py --config /etc/ai-monitor/config.json
```

The smoke check asserts: `/health` lists all four source IDs, `/api/v1/usage`
returns `observations: []` before any source succeeds, history reports `hours`
and `truncated`, every response is `Cache-Control: private, no-store`, no
response contains credential-looking text, responses stay fast while providers
fail, a non-loopback address cannot reach the port, and each of the four read
endpoints answers 200 within the response cap — the cap of the deployment named
by `--config`, or the built-in default when none is given. A 413 fails the check
rather than passing quietly, because a widget has to be able to fetch these.

A fresh data directory is used unless `AI_MONITOR_SMOKE_DIR` names one. Reusing a
directory accumulates observations and makes the printed history size grow
between runs, so the sizes are only comparable on a fresh directory.

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
configuration problem), `1` anything else.

The streams need different credentials, so a single-credential deployment shows
exactly one stream `not_configured` — which is the intended outcome, not a bug:

| Credentials present | `openrouter-account` (`/api/v1/credits`) | `openrouter-key` (`/api/v1/key`) |
| --- | --- | --- |
| management/provisioning key only | collects the account balance | `not_configured` — the current-key stream needs its own key |
| inference key only | falls back to the inference key, which the credit endpoint may refuse with `authorization` | collects the current key's usage |
| both | collects the account balance | collects the current key's usage |

Absent fields stay `null`: an account with no spend limit reports
`limitUsd: null` and an unknown budget pace rather than a fabricated zero.

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
# The home path must match the unit: deploy/ai-monitor.service lists
# /home/ai-monitor in ReadWritePaths= because ProtectSystem=strict would
# otherwise make it read-only and the Codex CLI could not write ~/.codex.
# ReadWritePaths= requires the path to exist, so use this exact --home-dir.
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

# 4. Unit, then start. Confirm the service account's home is writable first:
# the unit fails to start if /home/ai-monitor does not exist, and the Codex
# child fails if it exists but is not writable.
id ai-monitor && sudo -u ai-monitor test -w "$(getent passwd ai-monitor | cut -d: -f6)" \
  && echo "home is writable"
sudo install -m 0644 deploy/ai-monitor.service /etc/systemd/system/ai-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-monitor
```

Give the service account its own provider sign-in before expecting data:

```sh
# Codex state and credentials live in the service account's own ~/.codex, and
# the unit's ProtectSystem=strict makes $HOME read-only unless it is listed in
# ReadWritePaths= — which deploy/ai-monitor.service does for /home/ai-monitor.
# Verified on 2026-10-19: `codex app-server` dies outright when $HOME is
# read-only, and fails with "codex account authentication required" when that
# home has no ~/.codex/auth.json. So sign the *service account* in, not the
# operator (step 4 checks the home is writable):
sudo -u ai-monitor codex login      # or copy an auth.json into /home/ai-monitor/.codex

# Codex, default (stdio): nothing else to prepare. The service starts
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

The log filter redacts credentials, tokens, email addresses, and opaque session
strings, and it masks every credential the loaded configuration holds by exact
match — the shape patterns alone are a heuristic and cannot carry the spec's
unconditional "no credentials in logs or responses". A credential must never
appear in `journalctl` output or in a response body:

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
# The state directory is created by StateDirectory= in the unit, but the backup
# target is not: `install` fails with "Not a directory" rather than creating it.
sudo install -d -m 0700 -o ai-monitor -g ai-monitor /var/lib/ai-monitor/backups

# Online backup: safe while the service runs, because SQLite takes the locks
# itself. Write straight into the backup directory so no second copy is left
# loose in the state directory.
# The timestamp includes the time: `.backup` overwrites its target silently, so
# a second backup on the same day would destroy the first.
BACKUP="/var/lib/ai-monitor/backups/usage-$(date -u +%FT%H%M%SZ).db"
sudo -u ai-monitor sqlite3 /var/lib/ai-monitor/usage.db ".backup '$BACKUP'"
sudo chmod 0600 "$BACKUP"
sudo -u ai-monitor sqlite3 "$BACKUP" "pragma integrity_check;"
```

Also back up `/etc/ai-monitor/config.json` to a restricted location (it holds
credentials) and `deploy/ai-monitor.service`. Never store the config in a
world-readable directory or a git remote.

Restore:

```sh
sudo systemctl stop ai-monitor
# Check the backup before removing anything: the rm below is destructive, and a
# mistyped path should fail here rather than after the sidecars are gone.
RESTORE="/var/lib/ai-monitor/backups/usage-YYYY-MM-DDTHHMMSSZ.db"
test -s "$RESTORE" || { echo "no such backup: $RESTORE"; exit 1; }
sudo -u ai-monitor sqlite3 "$RESTORE" "pragma integrity_check;"
# Clear stale WAL sidecars: a leftover -wal belongs to the file being replaced
# and has no business being replayed over the restored one.
sudo rm -f /var/lib/ai-monitor/usage.db-wal /var/lib/ai-monitor/usage.db-shm
sudo -u ai-monitor sqlite3 /var/lib/ai-monitor/usage.db ".restore '$RESTORE'"
# Confirm the restored file before starting on it.
sudo -u ai-monitor sqlite3 /var/lib/ai-monitor/usage.db \
  "pragma integrity_check; select count(*) from observations;"
sudo systemctl start ai-monitor
```

`tools/backup_restore_check.py` runs the same procedure — including the
`install -d`, `.backup` and `.restore` invocations above — against a live service
in a temporary directory, so it can be checked before relying on it on a host:

```sh
PYTHONPATH=. python3 tools/backup_restore_check.py
```

It needs the `sqlite3` CLI and a working `codex`, and exits `2` when either is
missing so a skip cannot look like a pass. The journal check above still needs
the unit installed under systemd.

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
sudo tailscale serve --bg --https=443 http://127.0.0.1:8732
tailscale serve status
```

`tailscale serve status` and `tailscale funnel status` run without `sudo` on this
host, so the operator can confirm state before and after the change; only
`tailscale serve` itself needs privileges.

### Host state as surveyed (2026-10-19)

| Fact | Value |
| --- | --- |
| Node | `zoey` (`100.108.85.109`) |
| Tailnet domain | `zoey.tail831c33.ts.net` |
| Served URL after this change | `https://zoey.tail831c33.ts.net/` (port 443 needs no suffix) |
| Ports already served, tailnet only | `3773`, `8443`, `8444`, `8445` |
| Port 443 | free — not among the served mappings, and `ss -ltn` shows nothing listening on it |
| `127.0.0.1:8732` | free — the service's `api_port`, per `ss -ltn` |
| Funnel | off. `tailscale funnel status` lists every mapping as `(tailnet only)` |
| HTTPS certificates | enabled in the tailnet — the existing Serve mappings already use `ts.net` names |

The served-mapping rows come from `tailscale serve status`; the port-availability
rows come from `ss -ltn`, which is a different command and a different claim.

### Restricting access to identity and devices

Tailscale Serve inherits the tailnet policy file, so "restrict to the intended
identity and devices" is a policy-file change in the admin console, not a flag on
`tailscale serve`. Two things about that file matter before editing it:

- It is *evaluated* deny-by-default, but the policy a tailnet starts with allows
  everything, and rules union rather than override. Adding a grant for port `443`
  does not narrow a broader allow rule that is already there, so the existing
  policy has to be read in the console first, and the change is to narrow it.
- Not every name is a valid selector. A bare node name works only if the policy
  defines it under `hosts`; users are login names such as
  `papaya.ch0mp@gmail.com`. A snippet using `zoey` with no `hosts` entry, or
  `papaya.ch0mp@`, is rejected by the console.

With that in mind, the shape to adapt is:

```jsonc
{
  "hosts": {
    "zoey": "100.108.85.109"          // or dst may name a tag or the IP directly
  },
  "grants": [
    {
      "src": ["papaya.ch0mp@gmail.com"],   // narrow this to the devices in mind
      "dst": ["zoey"],
      "ip": ["tcp:443"]
    }
  ]
}
```

Treat it as a starting point, not a verified change: the current policy is not
visible from the CLI, the console may reject it as written against that policy,
and the four existing Serve mappings suggest the intended devices already reach
this node on other ports. Confirming which rule governs the new port is part of
the task, not a step that can be skipped.

### Acceptance checks (operator present)

1. **Intended client — macOS.** From `jamess-macbook-pro`, open
   `https://zoey.tail831c33.ts.net/api/v1/version` and confirm the version JSON.
2. **Intended client — iPhone.** From `iphone182-2`, open
   `https://zoey.tail831c33.ts.net/health`.
3. **Denied device.** From a device the policy does *not* allow to reach `zoey` —
   a tagged service node such as `rumi`, `happier`, or `mira` is the natural
   choice, since those are not granted access to user devices — confirm the
   connection fails rather than returning usage data.
4. **Funnel stays off.** `tailscale funnel status` must keep showing
   `(tailnet only)` for every mapping, including the new one.

Steps 1-3 need the devices and the policy edit, so they are executed with the
operator present. Step 4 and the survey above were run without privileges.
