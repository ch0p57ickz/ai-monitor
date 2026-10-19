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
# From the repository root, with the package importable. Starts the service on a
# loopback port with a fresh temporary data directory, uses the real adapters, and
# exercises the HTTP contract. Exits non-zero if a check fails, and removes the
# temporary directory unless AI_MONITOR_SMOKE_DIR names it.
PYTHONPATH=. python3 tools/smoke_check.py

# To check a deployment's own settings, point it at that deployment's config. Its
# adapters, provider binaries, and credentials are used, but data_dir and api_port
# are overridden, so the running service and its database are untouched. Reading
# /etc/ai-monitor/config.json (mode 0640) needs root, and sudo resets the
# environment, so pass PYTHONPATH through explicitly.
sudo -E env PYTHONPATH=. python3 tools/smoke_check.py --config /etc/ai-monitor/config.json
```

The smoke check **asserts**: every response is `Cache-Control: private, no-store`;
no response contains credential-looking text; each of the four read endpoints
(`/api/v1/version`, `/health`, `/api/v1/usage`, and a small history query) answers
200 within the response cap; and a non-loopback address cannot reach the port. The
cap is the one from the deployment named by `--config`, or the built-in default
when none is given. A 413 fails the check rather than passing quietly, because a
widget has to be able to fetch these.

It also **reports** what it saw, without asserting it: the `/health` source IDs and
statuses, whether `/api/v1/usage` was still empty before any source succeeded, the
history `hours` and `truncated` fields, and each response's size.

With `codex_transport = "socket"`, a `--config` run attaches to the same app-server
socket the running service uses. A fresh data directory is used unless
`AI_MONITOR_SMOKE_DIR` names one; reusing a directory accumulates observations and
makes the printed history size grow between runs, so sizes are comparable only on
a fresh directory. A fresh directory holds one observation per source, not the
populated payloads a long-running deployment has.

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
#
# Copy with an allowlist, not a denylist. The checkout holds a `.env` at mode
# 0664, and a plain `rsync -a` of the tree copies it into this 0775 directory,
# where it is readable by every local user — rehearsed, and it leaks. Naming what
# to copy fails safe instead: a new secret file is not copied unless it is named,
# whereas one missing from a denylist leaks silently.
#
# The copy runs as root for two reasons: the service account cannot traverse the
# operator's home (0750 on this host), so `sudo -u ai-monitor git clone` is denied
# before it starts; and there is no remote to clone from, since this repository has
# none. rsync also sidesteps git's dubious-ownership check, which a root-run git
# inside an ai-monitor-owned tree trips.
#
# `--exclude '*'` is what protects the virtualenv: rsync treats excluded paths as
# protected from --delete, so `venv/` survives an upgrade while a module removed
# from the package does not.
sudo install -d -o ai-monitor -g ai-monitor /opt/ai-monitor
sudo rsync -a --delete \
  --include '/ai_monitor/***' --include '/deploy/***' --include '/docs/***' \
  --include '/tools/***' --include '/pyproject.toml' \
  --exclude '*' \
  /path/to/this/checkout/ /opt/ai-monitor/
sudo chown -R ai-monitor:ai-monitor /opt/ai-monitor

# Confirm nothing secret came along. Anything that reaches this tree is readable
# by every local user, and this repository's `.env` is the file to expect.
sudo test ! -e /opt/ai-monitor/.env && echo "no .env in the deployment tree"

sudo -u ai-monitor python3 -m venv /opt/ai-monitor/venv

# Clear build artifacts from any earlier install on this tree. `pip install <dir>`
# reuses the `build/` directory it leaves, so a module deleted since then would be
# packaged again; the rsync cannot clear it, because excluded paths survive
# --delete. There is nothing to clear on a first install, and the step is safe to
# repeat.
sudo rm -rf /opt/ai-monitor/build

sudo -u ai-monitor /opt/ai-monitor/venv/bin/pip install /opt/ai-monitor

# Confirm the console script is on PATH inside the venv before going further.
# The unit runs this exact binary, and `ai-monitor --version` must print the
# release you just installed. Skipping this step is what leaves a unit that can
# never start: /opt/ai-monitor empty, no venv, no binary.
sudo -u ai-monitor /opt/ai-monitor/venv/bin/ai-monitor --version

# 3. Configuration with credentials, readable only by the service account.
sudo install -d -m 0750 -o root -g ai-monitor /etc/ai-monitor
sudo install -m 0640 -o root -g ai-monitor deploy/config.example.json /etc/ai-monitor/config.json
sudoedit /etc/ai-monitor/config.json   # set codex_bin, the agy path, keys

# Generate the account digest key used to derive opaque account ids. Without
# it, account-scoped trends (same-account pace, spend deltas) are disabled and
# the service logs a warning at startup. It must be at least 16 characters.
python3 -c 'import secrets; print(secrets.token_hex(32))'
# Then set "account_digest_key" to that value in the config.

# 4. Unit. Install it *from this repository*: a stale copy is a real failure mode.
# This host was found running a unit predating ReadWritePaths=/home/ai-monitor,
# which leaves the Codex child unable to write ~/.codex under ProtectSystem=strict.
# `diff` against the file in the repository shows any drift.
sudo install -m 0644 deploy/ai-monitor.service /etc/systemd/system/ai-monitor.service
sudo systemctl daemon-reload

# Check the unit before starting it. `systemd-analyze verify` names the exact
# problem the unit would hit — on this host it printed "Command
# /opt/ai-monitor/venv/bin/ai-monitor is not executable" while /opt/ai-monitor
# was still empty, which is precisely the state that produces status=203/EXEC.
sudo systemd-analyze verify /etc/systemd/system/ai-monitor.service

# 5. Check every precondition, then start. Each line below corresponds to a
# failure observed on this host, and each was silent until the journal was read:
#
#   binary missing or not executable  -> status=203/EXEC
#   config missing or unreadable      -> "config file not found", exit 2
#   service account's home unwritable -> the Codex child cannot write ~/.codex
#   a secret copied into the tree     -> world-readable credentials
#
# The config check runs as the service account deliberately. `sudo test -r` runs
# as root and passes no matter what the service will actually see.
id ai-monitor
sudo test -x /opt/ai-monitor/venv/bin/ai-monitor && echo "binary executable: ok"
sudo -u ai-monitor test -r /etc/ai-monitor/config.json \
  && echo "config readable by ai-monitor: ok"
sudo -u ai-monitor test -w "$(getent passwd ai-monitor | cut -d: -f6)" \
  && echo "home writable: ok"
sudo test ! -e /opt/ai-monitor/.env && echo "no .env in the deployment tree: ok"

# Any line missing above means fixing that thing before starting. A tree copied
# without the step 2 allowlist also carries `.gitignore`, `tests/`, `openspec/`,
# `build/`, and `.pytest_cache/`; none belong here, and `.env` is the one that
# matters, because this repository keeps it at mode 0664 and /opt is
# world-traversable:
#
#   sudo rm -f /opt/ai-monitor/.env
#   sudo test ! -e /opt/ai-monitor/.env && echo "gone"

sudo systemctl enable --now ai-monitor

# `enable --now` returns success even when the unit fails immediately, and
# `is-enabled` still reads "enabled" while it crash-loops, so check that it is
# actually running. This host reached 2141 restarts without anyone noticing.
systemctl is-active ai-monitor
systemctl status ai-monitor --no-pager
journalctl -u ai-monitor -n 20 --no-pager
```

If the unit is not `active`, the journal says why:

- `status=203/EXEC` means the binary in `ExecStart` cannot be executed — almost
  always a missing or incomplete step 2.
- `status=2` with `config error: config file not found` means step 3 was skipped,
  or the file is not readable by the service account.

`StartLimitBurst` stops the unit after ten failed starts in five minutes and reports
it as failed, so a broken deployment fails visibly instead of restarting forever.
That is the intended behaviour, not a second fault. After fixing the cause, clear
the counter and start again:

```sh
sudo systemctl reset-failed ai-monitor
sudo systemctl start ai-monitor
systemctl is-active ai-monitor
```

### Installing without network access

`pip install /opt/ai-monitor` builds the package with build isolation, which
installs `setuptools>=68` into a temporary build environment. A Python 3.12 venv
has no setuptools of its own, so with no index the install fails:

```
pip subprocess to install build dependencies did not run successfully
```

Do not work around that with `--system-site-packages`. It adds the system
site-packages to the venv's path, and pip then treats a system-wide copy of
`ai-monitor` as already installed, so an upgrade can report success while leaving
the old version in place — the failure this section exists to avoid.

Build a wheel where setuptools is available, then install the wheel. Installing a
wheel builds nothing, so the service host needs neither setuptools nor an index:

```sh
# Build the wheel somewhere that has setuptools. --no-build-isolation uses that
# interpreter's setuptools rather than downloading one, so it works offline; on a
# host with network access you can omit the flag instead. Write the wheel outside
# the checkout, or `dist/` appears as an untracked directory in /opt/ai-monitor.
python3 -m pip wheel --no-build-isolation -w /tmp/ai-monitor-wheel /opt/ai-monitor

# Copy it to the service host, then install into the clean venv:
sudo -u ai-monitor /opt/ai-monitor/venv/bin/pip install --no-index /tmp/ai-monitor-wheel/ai_monitor-*.whl
sudo -u ai-monitor /opt/ai-monitor/venv/bin/ai-monitor --version
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
# Copy the revision you want into place, as root, then hand it back to the
# service account. Same allowlist as the install: a denylist would copy the
# checkout's `.env` into this tree, where it is readable by every local user. The
# service account cannot reach the operator's checkout, and a root-run git inside
# an ai-monitor-owned tree trips git's dubious-ownership check, so rsync is the
# form that works without extra configuration. `--exclude '*'` protects `venv/`
# from --delete.
sudo rsync -a --delete \
  --include '/ai_monitor/***' --include '/deploy/***' --include '/docs/***' \
  --include '/tools/***' --include '/pyproject.toml' \
  --exclude '*' \
  /path/to/this/checkout/ /opt/ai-monitor/
sudo chown -R ai-monitor:ai-monitor /opt/ai-monitor
sudo test ! -e /opt/ai-monitor/.env && echo "no .env in the deployment tree"

# Clear the build artifacts first. `pip install <dir>` builds in place, and
# setuptools reuses the `build/` directory it leaves behind: a module deleted or
# renamed in this revision stays in `build/lib/` and is packaged again, so the
# installed package can carry code from the previous revision. The rsync above
# cannot clear it, because excluded paths survive --delete. Verified on this host
# by installing a tree, adding a module, installing again, deleting the module
# from the tree, and finding a reinstall still contained it; removing `build/`
# first made the same reinstall clean.
sudo rm -rf /opt/ai-monitor/build

sudo -u ai-monitor /opt/ai-monitor/venv/bin/pip install /opt/ai-monitor
# Without network access, install a wheel built as described under "Installing
# without network access" instead. Do not add --system-site-packages to the venv
# to make this work: pip then treats a system copy as already installed and can
# skip the upgrade while reporting success.
sudo systemctl restart ai-monitor
systemctl status ai-monitor --no-pager

# Confirm the running release is the one you checked out. `pip show`, `--version`,
# and the API all read the installed distribution, so all three agree; verified by
# packaging 0.2.0 over 0.1.0 and back.
sudo -u ai-monitor /opt/ai-monitor/venv/bin/ai-monitor --version
curl -s http://127.0.0.1:8732/api/v1/version
```

With git history on the host instead of an rsync copy, run the git steps as root
and hand the tree back, since the service account cannot reach the source:

```sh
sudo git -c safe.directory=/opt/ai-monitor -C /opt/ai-monitor fetch --tags
sudo git -c safe.directory=/opt/ai-monitor -C /opt/ai-monitor checkout <tag-or-sha>
sudo chown -R ai-monitor:ai-monitor /opt/ai-monitor
```

Schema changes are applied on open; take a backup first (below). A failed
upgrade is reverted by checking out the previous revision and repeating the
`pip install` and restart.

Rollback is the same three steps with the earlier revision, and the same version
check confirms it took: check out the previous `<tag-or-sha>`, reinstall, restart,
then compare `--version` and `/api/v1/version` against the release you intended.
A rollback that leaves the service announcing the newer version means the install
did not replace the package.

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

Rollback is the upgrade with an earlier revision, so the copy, install, and
restart steps are the ones above. What differs is how the earlier revision gets
into place.

**The deployed tree carries no git history.** The allowlist copy leaves `.git`
out on purpose (it is not in the include list), so `/opt/ai-monitor` is not a
repository and `git -C /opt/ai-monitor` fails there. It also cannot run as the
service account: that account cannot reach the source checkout. On this layout,
check out the earlier revision in the operator's own checkout and repeat the
Upgrade copy and install steps against it:

```sh
cd /path/to/this/checkout
git checkout <previous-good-sha>
# Then repeat the rsync, chown, pip install, and restart steps under Upgrade.
```

With git history on the host instead — a tree that was cloned rather than
copied — the git steps run as root, because a root-run git inside an
ai-monitor-owned tree trips git's dubious-ownership check, and then the tree goes
back to the service account:

```sh
sudo git -c safe.directory=/opt/ai-monitor -C /opt/ai-monitor fetch --tags
sudo git -c safe.directory=/opt/ai-monitor -C /opt/ai-monitor checkout <previous-good-sha>
sudo chown -R ai-monitor:ai-monitor /opt/ai-monitor
# Same staleness rule as the upgrade: setuptools reuses `build/`, so clear it or a
# module the earlier revision deleted stays packaged.
sudo rm -rf /opt/ai-monitor/build
sudo -u ai-monitor /opt/ai-monitor/venv/bin/pip install /opt/ai-monitor
sudo systemctl restart ai-monitor
```

Either way, confirm the running release is the one you intended. A service that
still announces the newer version means the install did not replace the package:

```sh
sudo -u ai-monitor /opt/ai-monitor/venv/bin/ai-monitor --version
curl -s http://127.0.0.1:8732/api/v1/version
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

## Tailscale Serve (task 5.3, deployed 2026-10-19)

The service binds `127.0.0.1` only. Publication is via Tailscale Serve, never
Funnel:

```sh
sudo tailscale serve --bg --https=443 http://127.0.0.1:8732
tailscale serve status
```

`tailscale serve status` and `tailscale funnel status` run without `sudo` on this
host, so the operator can confirm state before and after the change; only
`tailscale serve` itself needs privileges.

### Deployed state (read back from the node, 2026-10-19)

`zoey` is tagged and the API is published as a **Service**, `svc:ai-monitor`. Read
the state back from the node rather than the console:

```sh
tailscale serve status            # what is published, and whether it is tailnet-only
tailscale funnel status           # must stay "(tailnet only)"
tailscale status --json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"].get("Tags"))'
tailscale debug netmap | python3 -c '
import json, sys
caps = json.load(sys.stdin)["SelfNode"]["CapMap"]
print("service-host:", caps.get("service-host"))
print("services:", caps.get("services/ai-monitor"))
'
```

Observed:

| Fact | Value |
| --- | --- |
| Self tag | `tag:zoey` |
| Service | `svc:ai-monitor`, endpoint `tcp:443`, proxying `http://127.0.0.1:8732` |
| Service VIP | `100.76.182.227` (`fd7a:115c:a1e0::6528:b6e4`), from `SelfNode.CapMap["service-host"]["svc:ai-monitor"]` |
| DNS | `ai-monitor.tail831c33.ts.net` resolves to the VIP |
| Publication | `(tailnet only)`; Funnel off for every mapping |
| The four earlier routes | `3773`, `8443`, `8444`, `8445` still published and tailnet-only; `8443` and `8444` answer HTTP 200 from the host |
| Service access | one packet-filter rule for the VIP, admitting the operator's devices and no `tag:agent` node |
| Device rules | the allow-all device rule is untouched, which is why the four earlier routes are unaffected |
| Backend | nothing listens on `8732` until task 5.2 starts the service, so the Service answers 502 in the meantime |

The Service path restricts anything at all for a reason visible in the packet
filter: `*` in a grant expands to *device* addresses, so the allow-all rule that
covers every device on every port does **not** cover a Service VIP. A Service is
reachable only through its own grant, which is what makes the denial check
possible without narrowing the device rules.

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
identity and devices" is a policy change in the admin console, not a flag on
`tailscale serve`.

#### What the current policy does (verified, not assumed)

The policy file is not readable from the CLI, but its *effect on this node* is.
`tailscale debug netmap` runs without privileges and returns the packet filter
the control plane distributed to `zoey`:

```sh
tailscale debug netmap \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["PacketFilterRules"])'
```

On 2026-10-19 that printed a single rule — sources spanning the whole CGNAT range,
destinations every port:

```text
SrcIPs   100.115.94.0-100.127.255.255, 100.64.0.0-100.115.91.255, fd7a:115c:a1e0::/48
DstPorts IP "*", ports 0-65535
```

So this tailnet is on the default allow-all policy: every member device already
reaches `zoey` on every port. That includes the eight `tag:agent` service nodes
(`rumi`, `happier`, `mira`, `ranger`, `grafana`, `livesync`, `hermes`,
`openclaw-gx10`), which belong to the separate `tagged-devices` identity. The
operator's own devices belong to `papaya.ch0mp@gmail.com` and include
`jamess-macbook-pro`, five iPhones and iPads, `aerith`, `aloy`, `moltbot`, and
`pineapple`.

#### Why adding a rule cannot fix that

Access rules union and the only action is `accept` — there are no deny rules. A new
rule granting `zoey:443` to the operator's devices would be *added* to the
allow-all rule that already covers it, so it narrows nothing. The allow-all rule
has to be replaced by explicit grants, which means the served port must be
*excluded* from the broad rule. A destination cannot be negated, so that exclusion
is global: the broad rule grants every port except `443` on every host.

One consequence follows, and it is worth checking before applying the change: if
any other node serves HTTPS on `443` inside the tailnet, it becomes unreachable and
needs a grant of its own. If that is a problem, serve the widget API on a dedicated
port instead. `9443` is free on `zoey` (`3773`, `8443`, `8444`, `8445` are taken)
and nothing else is likely to use it, which makes the exclusion harmless. Change
the port in the `serve` command below and in both rules.

#### The change to make

`deploy/tailnet-policy.example.hujson` holds the policy fragment with the reasoning
inline. Merge it into the existing policy file rather than replacing the file: if
that file already defines `groups`, `tagOwners`, `nodeAttrs`, or `grants`, keep them
and replace only the rule that grants `*` to every port. Two details the console
enforces and that are easy to get wrong:

- A bare node name is a valid destination only if the policy defines it under
  `hosts`. The example defines `"zoey": "100.108.85.109"` for that reason.
- Users are login names. `papaya.ch0mp@gmail.com` is the operator
  (`tailscale status --json`, field `User.*.LoginName`); the truncated
  `papaya.ch0mp@` is rejected. A `src` naming a user selects *every* device that
  user owns — here the Mac, the iPhones, and the operator's other devices — which
  is the tailnet-identity restriction the requirement asks for. To pin it to two
  devices instead, add `hosts` aliases for their Tailscale IPs and list those.

The example also carries a `tests` block. The admin console runs those assertions
when the file is saved, so saving is the first real check of the change:

```text
papaya.ch0mp@gmail.com -> 100.108.85.109:443   accept
tag:agent              -> 100.108.85.109:443   deny
tag:agent              -> 100.108.85.109:8443  accept
```

The third assertion is deliberate: it fails if the change ever narrows `zoey` as a
whole instead of `zoey:443`, which is what would break the four existing routes.

#### Verify the denial from a device

The policy tests are assertions about rules. The requirement asks for a device
test, so also check from a real disallowed device — a `tag:agent` node such as
`rumi` or `mira`:

```sh
curl -s --max-time 5 https://zoey.tail831c33.ts.net/api/v1/version   # must fail
curl -s --max-time 5 https://zoey.tail831c33.ts.net:8443/            # must still work
```

A denied connection is dropped rather than refused, so expect a timeout rather
than a connection error. Re-running `tailscale debug netmap` on a disallowed node
is the second half of the check: its filter should no longer list `zoey:443`.

#### The path in use: publish the API as a Tailscale Service

**This is what `zoey` runs.** The node-route form above remains the alternative,
and is the one to use if tagging the host is not acceptable.

A Service is the cleaner mechanism for this restriction, and it avoids the global
port exclusion above entirely. Service access is granted explicitly — a
`dst: ["svc:<name>"]` rule — so the denial is a positive statement rather than an
allow-all that has to be dismantled. Tailscale's wording is that users and devices
"must explicitly be granted access to a service via Grants". The `*` wildcard
expands to *device* addresses and approved subnet routes, not to a Service's
TailVIP, so the default allow-all policy does not reach a Service and no other
node's rules are affected.

The cost is one requirement, stated as a warning in the Services documentation:
**a Service host must use a tag-based identity.** A device authenticated with a
user account cannot host a Service. `zoey` is user-owned and untagged, so it has to
be tagged, and the Service has to be advertised from `zoey` itself, because the API
binds `127.0.0.1` and no other host can reach it to proxy for it.

What tagging `zoey` does and does not change, checked against the documentation
rather than assumed:

- **Identity headers are unaffected.** Serve fills `Tailscale-User-Login` from the
  *requester's* identity; the docs say only that these headers are not populated
  for traffic *originating from* tagged devices. Requests from the Mac and iPhone
  still arrive with a login name, so `zoey`'s four existing routes keep the
  identity behaviour they have today.
- **Tailscale SSH is unaffected here.** `tailscale debug prefs` reports
  `RunSSH: false` on `zoey`, so there is no Tailscale SSH session to lose.
- **`autogroup:self` stops matching `zoey`.** A tagged device is not "a user's
  device", so `dst: ["autogroup:self"]` and `dst: ["papaya.ch0mp@gmail.com"]` no
  longer cover it. Nothing breaks under today's allow-all policy; a later
  narrowing of the device rules has to name the tag instead.
- **Tagging re-registers the node.** The DNS name, IP, and the four Serve routes
  persist, but the node is re-authenticated as the tag. Do this with LAN or console
  access to `zoey` in hand rather than only over the tailnet, in case the session
  drops before re-authentication completes.

#### The one risk in the policy edit, and how to check it first

Service access needs a `grants` entry. Whether adding one can change anything else
depends on a detail the documentation gets wrong, so check it rather than assume it.

The docs say the default allow-all policy applies "in the absence of an `acls`
section". Tailscale's issue tracker says that is not the behaviour. The default
policy is filled in only when the file is blanked *entirely*, not section by
section, and what actually decides it is whether the file contains any allow rule at
all: "the absence of any allow rules enables global default-allow"
([tailscale#2799][issue-2799]). If a `grants` section counts as an allow rule, adding
one to a file with no `acls` section turns the tailnet deny-by-default — which would
cut every device-to-device connection, including `zoey`'s four routes.

There is a safe way to learn which case this tailnet is in, without editing anything.
In the admin console's JSON editor, the **Convert to grants** button is enabled only
when the policy file contains an `acls` section.

- **Button enabled** — the file has an explicit allow rule, so adding a grant leaves
  every existing connection alone. Proceed.
- **Button disabled** — the file has no `acls` section, which is the ambiguous case.
  Write today's behaviour down explicitly in the same edit:

  ```json
  "acls": [ { "action": "accept", "src": ["*"], "dst": ["*:*"] } ]
  ```

  That is the default allow-all rule spelled out, so the result no longer depends on
  how an absent section is interpreted, and tightening it later is a deliberate edit
  instead of a side effect.

Either way, read the diff the console shows before it saves, and use **Preview
rules** to check how the rules apply to a given user or tag. This one file governs
every device on the tailnet, and an edit that tightens access can cut that access the
moment it saves.

[issue-2799]: https://github.com/tailscale/tailscale/issues/2799

The steps, in order:

```sh
# 1. Define the tag in the policy file so you can apply it. Without this, step 2 is
#    rejected with "requested tags are not permitted":
#    "tagOwners": { "tag:zoey": ["papaya.ch0mp@gmail.com"] }

# 2. Tag the host. This is a `tailscale up` flag, not a `tailscale set` one:
#    `tailscale set` has no --advertise-tags and answers "flag provided but not
#    defined".
#
#    `up` is not a merge. Its help says "If flags are specified, the flags must be
#    the complete set of desired settings", and it stops rather than silently
#    resetting a setting you leave out. This host has one non-default setting —
#    OperatorUser is `jp` — so name that too, or `up` exits with "requires
#    mentioning all non-default flags". When it exits, it prints the command to run
#    as "The following flags are missing: ...". Run that.
sudo tailscale up --advertise-tags=tag:zoey --operator=jp
tailscale status --json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["Tags"])'

#    Tagging changes the node's ownership, so the same command may print a
#    re-authentication URL; complete it in a browser signed in as the tag owner.
#    --force-reauth is for a stale node key, which is a different problem from the
#    missing-flags error above.

# 3. Define the Service in the admin console (Services page): name it and give it
#    the endpoint tcp:443. This needs Owner, Admin, or Network admin. To skip the
#    manual approval in step 4, add an auto-approver in the same policy file:
#    "autoApprovers": { "services": { "svc:ai-monitor": ["tag:zoey"] } }

# 4. Advertise it from zoey, then approve the host in the console — or add an
#    autoApprovers.services rule to pre-approve it.
sudo tailscale serve --service=svc:ai-monitor --https=443 127.0.0.1:8732
tailscale serve status

# 5. Grant access. This one rule is the whole access control story:
#    {"grants": [{"src": ["papaya.ch0mp@gmail.com"],
#                 "dst": ["svc:ai-monitor"], "ip": ["443"]}]}
```

The URL becomes `https://ai-monitor.tail831c33.ts.net/` — the Service's MagicDNS
name, not `zoey`'s. Services are TCP-only, which HTTPS satisfies, and a layer 7
endpoint (`--https`) is the kind that carries identity headers; a layer 3 endpoint
(`--tun`) does not. Clients on v1.94 and later need no extra configuration, so the
Mac and iPhone qualify without `--accept-routes`.

A Service is also a valid `accept` or `deny` destination in the policy `tests`
block, so the denial is asserted the same way as in the node-route form:

```json
{"src": "tag:agent", "deny": ["svc:ai-monitor:443"]}
```

**Spec note.** Task 5.3 asks to "publish the local port through Tailscale Serve over
HTTPS and restrict access through tailnet identity and device rules". A Service is
advertised *with* `tailscale serve` and restricted by a grant, so it meets the
intent, and it satisfies the denied-device scenario without the collateral of the
node-route form. The wording should name the Service explicitly so the acceptance
criteria and the implementation agree.

The agent dashboards are reported to run as Services for the same reason. This
node now collects Service information — `tailscale debug netmap` reports
`CollectServices: true` — and `SelfNode.CapMap` carries the Service identities
this node may host, with each VIP under `services/<name>[0].Addrs[0]`. So Service
identity and VIPs are readable from `zoey`, without the console.

### Acceptance checks (operator present)

1. **The Service answers at all.** Start the service first (task 5.2) — until it
   runs, nothing listens on `8732` and the Service returns 502. Then confirm the
   API locally: `curl -s http://127.0.0.1:8732/api/v1/version`.
2. **Intended client — macOS.** From `jamess-macbook-pro`, open
   `https://ai-monitor.tail831c33.ts.net/api/v1/version` and confirm the version
   JSON. The Service DNS name is the address, not `zoey.tail831c33.ts.net`: port
   `443` on `zoey` itself is not served.
3. **Intended client — iPhone.** From `iphone182-2`, open
   `https://ai-monitor.tail831c33.ts.net/health`.
4. **Denied device.** From a `tag:agent` service node such as `rumi`, `happier`, or
   `mira` — the natural choice, since those belong to the separate
   `tagged-devices` identity and the grant admits the operator's devices only —
   confirm the Service URL fails rather than returning usage data, and that
   `https://zoey.tail831c33.ts.net:8443/` still answers. This check is what the
   packet filter predicts; the device run is what proves it.
5. **Funnel stays off.** `tailscale funnel status` must keep showing
   `(tailnet only)` for every mapping, including the Service.

Steps 2-4 need the devices, so they run with the operator present. Step 5 and the
survey above run without privileges.
