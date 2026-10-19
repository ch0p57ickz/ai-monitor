# GX10 Capability Report (redacted)

Date: 2026-10-19
Verified on: `zoey` (100.108.85.109), Tailnet `tail831c33.ts.net` (login `papaya.ch0mp@gmail.com`)
Operator: `jp`

This report supports task 1.1 of `openspec/changes/gx10-usage-service`. It lists
versions, bucket names, field presence, and endpoint error categories only. It
contains no secret values, no raw provider payloads, and no account identifiers.

## Host runtime

| Item | Value |
| --- | --- |
| OS | Ubuntu 24.04.5 LTS (Noble Numbat) |
| Python | 3.12.3 (`python3`, stdlib-only implementation target) |
| Service manager | systemd 255 (255.4-1ubuntu8.17), system state `running`; supports a dedicated unprivileged service unit with `Restart=always` |
| Tailscale | 1.102.4 (commit 3caf7d9e7dcaba589cfc58beda596929733e4fea); node `zoey` is active and connected |

Service manager decision: a systemd unit under a dedicated unprivileged account
satisfies the `gx10-service-access` spec (dedicated account, automatic restart,
persistent data directory).

## Codex app-server

| Item | Value |
| --- | --- |
| CLI | codex-cli 0.157.1 (`/usr/bin/codex`) |
| App-server subcommand | `codex app-server` (experimental): `daemon`, `proxy`, `generate-ts`, `generate-json-schema`, `--listen stdio://`/`unix://`/`ws://` |
| Control socket | `~/.codex/app-server-control/app-server-control.sock` exists while `codex app-server daemon` runs, but on this host it accepts then closes every connection. The official `codex app-server proxy` fails identically (`failed to copy data from stdin to socket: Broken pipe`), so the daemon — not the client — is at fault. A directly started `codex app-server --listen stdio://` works |
| Daemon root cause | `kernel.apparmor_restrict_unprivileged_userns = 1` (Ubuntu 24.04 default) blocks the user namespace that Codex's bubblewrap sandbox needs: `bwrap: setting up uid map: Permission denied`, and the daemon logs `Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces`. `kernel.unprivileged_userns_clone` is already `1`, so the AppArmor restriction is the blocker, not the userns sysctl |
| Daemon fix (needs root) | Either grant the binary a user-namespace AppArmor profile (targeted), or relax the restriction system-wide with `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (weakens host hardening). Neither is needed for the service: the stdio transport below avoids the daemon entirely |
| Transports | `codex_transport` selects how the adapter reaches the interface. `stdio` (default) starts `codex app-server --listen stdio://` as a child and speaks the same protocol over its pipes; `socket` uses the daemon control socket instead. The stdio path runs no sandboxed tools, so the AppArmor user-namespace restriction does not apply to it. `codex_bin` names the executable (default `codex`) |
| Auth state | `~/.codex/auth.json` present (contents not read, not reported) |
| Protocol | Confirmed from `codex app-server generate-json-schema` and a live session: the connection must send `initialize` first (any other request is answered with a closed connection), then `account/rateLimits/read` (`Account/rateLimits/readRequest`, response `GetAccountRateLimitsResponse`). Server notifications (`configWarning`, `account/updated`) arrive between replies and must be skipped |
| Rate-limit fields | `rateLimitsByLimitId` (keyed) with a `rateLimits` single-view mirror; each snapshot has `primary`/`secondary` `RateLimitWindow` objects carrying `usedPercent` (integer), `windowDurationMins`, and `resetsAt` (Unix seconds). `accountId` is defined but was absent in the live payload |

Live evidence (`tools/codex_live_check.py`, stdio transport, real account,
snapshot 2026-10-19): `view: keyed`, two windows — `5h · primary` at 25% used
against `expectedUsedPercent` 81.94 → `under`, and `7d · secondary` at 25%
used against `expectedUsedPercent` 27.4 → `at`; `accountId` was null in the
live payload. Percentages move with use; the classification and the presence
of real reset bounds are the point.

Verdict: capable, and usable on this host out of the box. The default
`codex_transport = "stdio"` needs no daemon and no host policy change, so the
AppArmor user-namespace restriction below is irrelevant to the service. Selecting
the `socket` transport instead reports the Codex source as
`network`/`unavailable` until that restriction is lifted for the daemon. One
deployment caveat applies to either transport: the service account needs a
writable `$HOME` for `~/.codex` — see "the unit's sandbox would have starved the
Codex child" below.

## Antigravity

| Item | Value |
| --- | --- |
| CLI | `agy` 1.4.2 (`~/.local/bin/agy`); `agentapi` shim at `~/.gemini/antigravity-cli/bin/agentapi` |
| `agentapi` subcommands | `get-conversation-metadata`, `new-conversation`, `send-message` only — no quota/usage subcommand exposed |
| Installed flag | `~/.t3/caches/antigravity.json` reports `installed: true`, `enabled: true`, `version: null`, `status` present, `auth.status` present (values redacted) |
| Known buckets | Session data only (per-workspace conversation mappings in `~/.gemini/antigravity-cli/cache/last_conversations.json`). No weekly quota bucket, no usage-percentage fields found in any readable location |
| Quota/limit fields | Absent in every inspected state file; `ANTIGRAVITY_LS_ADDRESS` is required for `agentapi` calls and is not set by default |

Verdict: per the `provider-usage-collection` spec, the Antigravity adapter must
treat this variant as **session-data-only** (retain reported group/model/session
scope, never invent a weekly window) and report `unavailable` until a compatible
source exposing quota buckets is observed.

Verified live through the adapter: `agy usage --json` exits 2 with
`Error: unexpected argument "usage".`, which the adapter classifies as
`unavailable` ("usage interface not supported by this variant"). The parser is
capability-aware: it turns only reported buckets into windows, so a session-only
variant never gains a fabricated weekly window, and a payload with no buckets is
also treated as `unavailable`.

## OpenRouter endpoints

The two current-key endpoints were probed **without credentials** to record error
categories. The account-credit endpoint requires a credential, so it is verified
through `tools/openrouter_live_check.py`, which reads credentials from the
environment only and never prints them.

Live result (2026-10-19, a management/provisioning key in `OPENROUTER_MGMT_KEY`,
no inference key):

| Stream | Endpoint | Result |
| --- | --- | --- |
| `openrouter-account` | `/api/v1/credits` | `success` — `totalCreditsUsd: 530.0`, `totalUsageUsd: 474.663`, `remainingUsd: 55.337`, with `limitUsd`, `usedUsd`, `resetsAt` all `null` and pace `unknown`/`none` |
| `openrouter-key` | `/api/v1/key` | `failed` — `not_configured` ("openrouter api key not configured"), its own health independent of the account stream |

That is the partial result the spec asks for: the account stream collected while
the current-key stream could not be called with the available credential, and
each kept its own status, error category, and freshness. The nulls matter too —
an absent credit limit and an absent per-key budget stay `null`, never inferred
as zero or full. With an inference key instead, the documented split reverses
(only the current-key stream succeeds); that direction is covered by unit tests
and by the invalid-key `authentication` probe rather than by a live run.

The two streams need different credentials by design, so a single-credential
deployment shows one stream `not_configured` rather than a fabricated value.
The spec requires exactly this: a successful result from either endpoint remains
available when the other "cannot be called with the available credential".

Account identity: neither OpenRouter endpoint exposes an identity that is safe to
store (the key payload's `label` may be a name or email), so `accountId` stays
`null` for both OpenRouter streams. Per the design, a null identity means
account-dependent trends are avoided: cross-snapshot spend deltas are not
combined, so budget pace `basis` stays `none`/`estimated` and never `reported`
on this host. Codex derives an opaque, keyed digest of the app-server account
identity when it reports one and `account_digest_key` is configured; the raw
identity is never stored. The delta path is exercised by tests with a synthetic
account id.

| Endpoint | Purpose | Unauthenticated result | Error category |
| --- | --- | --- | --- |
| `GET https://openrouter.ai/api/v1/key` | current-key usage/limits | HTTP 401, JSON `{"error":{"message":"No cookie auth credentials found","code":401}}` | `authentication` |
| `GET https://openrouter.ai/api/v1/auth/key` | legacy alias of current-key info | HTTP 401, JSON `{"error":{"message":"No cookie auth credentials found","code":401}}` | `authentication` |
| `GET https://openrouter.ai/api/v1/credits` | account credits (management/provisioning key) | Not probed (requires credential) | — |

Correction: an earlier draft of this report labelled `/api/v1/auth/key` as the
account-credit endpoint. `/api/v1/auth/key` is an alias of the current-key
endpoint; account credits come from `/api/v1/credits`. The adapters use
`/api/v1/credits` (account) and `/api/v1/key` (current key). `/api/v1/credits`
could not be probed without a management credential.

Credential-permission checks (required by task 1.1) are pending: once a key is
provided to the service account, the service must verify (a) whether the current
key can read `/api/v1/key` and (b) separately whether it (or a management key) can
read `/api/v1/credits`, and record each as an independent stream. A non-management
key calling `/api/v1/credits` is expected to return HTTP 403, which the adapter
reports as `authorization` (operator action) without affecting the current-key
stream. Until a key is provided, both streams start in
`operator_action`/`not_configured` health state. No shared `Retry-After` behavior
was observed in the 401 responses.

## Local smoke check (executed on this host)

The service was run against this host on a loopback port with a temporary data
directory, with the real adapters, and the API contract was exercised over
HTTP. Results:

| Check | Result |
| --- | --- |
| `/api/v1/version` | `{"api_version": "1", "service_version": "..."}` |
| `/health` | `degraded`; `codex` retrying (`unsupported`), `antigravity` unavailable, `openrouter-account` and `openrouter-key` `operator_action` (`not_configured`) |
| `/api/v1/usage` before any success | `observations: []` |
| `/api/v1/usage/history?hours=1&limit=5` | `hours: 1`, `truncated: false`, 0 observations |
| `Cache-Control` on every response | `private, no-store` |
| Credential-looking text in responses | none |
| Response latency | ~0.7 ms while providers were failing (API does not poll) |
| Non-loopback address | refused (Tailscale address `100.x` could not reach the port) |

Live evidence now exists for the soak across an observed reset and for the
OpenRouter split; the Tailscale access rules and the systemd install remain
pending (tasks 5.2-5.4).

### Finding: the unit's sandbox would have starved the Codex child

The first version of `deploy/ai-monitor.service` set `ProtectSystem=strict`,
which mounts the entire file system hierarchy read-only, `$HOME` included, while
listing only `/var/lib/ai-monitor` as writable. The bundled Codex CLI keeps its
credentials and state in `~/.codex`, so the stdio transport — the default — could
not have collected on a deployed host.

Checked against the real CLI (2026-10-19):

| Run | `$HOME` | Result |
| --- | --- | --- |
| Baseline | the operator's home | `success`, 2 quota windows |
| Fresh, writable home | empty new directory | `codex account authentication required` — the credentials live in `~/.codex` |
| Read-only home (mode 0555) | not writable | the child died without answering (`closed the connection`) |

The unit now lists `/home/ai-monitor` in `ReadWritePaths=`, and
`tests/test_deploy_unit.py` asserts it, so the invariant cannot be dropped
silently. The installer must create that home (`useradd -m`) before enabling the
unit, and the *service account* — not the operator — must be signed in to Codex.

Note on how this was checked: the sandbox could not be replicated for a test run
because systemd does not support `ProtectSystem=` in user services ("the
underlying kernel functionality is only accessible to privileged processes" —
`systemd.exec`). A `systemd-run --user` replica silently ignored the directive
and reported success, so the first attempt proved nothing; the read-only-`$HOME`
and credential tests above are what actually establish the requirement. The real
unit still needs a privileged run to confirm (task 5.2).

## Deployment procedure verification (tasks 5.2 and 5.4, unprivileged parts)

Tasks 5.2 and 5.4 need `sudo` and a deployed host, but most of what they ask for
is about the *procedure* rather than the host, and a procedure is better checked
before it is run as root. `tools/backup_restore_check.py` runs the runbook's
backup and restore commands in a temporary directory against the real service
and the real `sqlite3` CLI: it backs up online while the service runs, deletes
the database and its WAL sidecars, restores, compares the restored rows with what
was captured, and then starts a worker on the restored file to confirm the API
serves the cached observation. All of it passes on this host.

| Check | Result |
| --- | --- |
| Online backup while the service runs (`.backup`) | Pass — consistent, and it works with the `-wal` file present |
| Restore after losing the database and its sidecars (`.restore`) | Pass — `pragma integrity_check` reports `ok`, and every row matches the capture |
| A restart on the restored database serves cached data (5.2) | Pass — `/api/v1/usage` returns the restored observation |
| The example config loads through the real `Config` | Pass — and now covers every field the loader accepts |

### Finding: the documented backup command failed

The runbook's backup step ran `install -m 0600 ... /var/lib/ai-monitor/backups/`
without ever creating that directory, and it backed up into the state directory
before *copying* the result into `backups/`, leaving a stray database beside the
live one. Reproduced:

```
$ install -m 0600 usage.db /var/lib/ai-monitor/backups/
install: cannot create regular file '/var/lib/ai-monitor/backups/': Not a directory
```

The procedure now creates the directory with the right owner and mode, writes the
backup straight into it, clears stale `-wal`/`-shm` sidecars before a restore,
checks integrity before starting on the restored file, and lists `backups/` in
the layout table. `deploy/config.example.json` also omitted `pace_tolerance` and
`response_cap_bytes`, which the loader accepts; both are now present.

### Finding: `.backup` overwrites its target, so a same-day name loses backups

The procedure named the backup `usage-$(date +%F).db`. Verified on SQLite 3.45.1:
`.backup` opens its target for writing, truncates it, and reports success
without a word — a second backup on the same day therefore destroys the first,
which may be the good one. The name now carries the time
(`date -u +%FT%H%M%SZ`), the backup is integrity-checked as it is written, and
the restore validates the source with `test -s` *before* the destructive
`rm -f` of the sidecars rather than after.

### Finding: the check could pass on a fresh poll instead of the restore

The first version of the tool asserted only that the restarted service served at
least one observation. Starting a worker polls immediately, so a *fresh*
collection satisfied that assertion even when the restore had produced nothing —
demonstrated by disabling the restore, which still printed "observations served
after restore and restart: 1". The two errors making a correct restore fail and a
missing restore pass are the same mistake in opposite directions: the check was
not measuring what it claimed.

It now fingerprints the backup file rather than the live database (fingerprinting
the live database raced the next poll and could fail a correct restore), and the
restart phase runs with provider binaries that cannot execute, so the only
possible source of a served observation is the restored database. The served
`collectedAt` must match a restored row. With the restore disabled the check now
fails on three counts and exits 1.

### Finding: a provider could smuggle a configured credential into the database

The spec is unconditional: credentials "SHALL NOT appear in HTTP responses or
logs" (`gx10-service-access`), and successful observations "SHALL NOT persist
credentials" (`usage-snapshots`). Redaction was heuristic — it masks *shapes*
(`sk-…` keys, bearer tokens, emails, opaque strings) and its own docstring
records that an unkeyed 32+ character alphabetic run passes through, because that
shape is indistinguishable from a file path.

That is fine for diagnostics, but a heuristic cannot carry an unconditional
guarantee, and `normalize_observation` — the single point where provider data
becomes a stored observation — redacted only `notes`. Everything else the
provider chose went in untouched. Reproduced with a local stand-in for
`/api/v1/key` that echoes the configured key back in `limit_reset_at`:

```
canary A in observation payload: True      ← served by /api/v1/usage
canary A persisted in SQLite:    True      ← and durable
```

The same value reaches a widget payload. Both mechanisms are now in place:

- configured credentials (`openrouter_api_key`, `openrouter_management_key`,
  `account_digest_key`) are **registered** and masked by exact match, which does
  not depend on guessing the shape;
- `normalize_observation` redacts provider-controlled text (`label`, `group`,
  `model`, `scope`, and the reset timestamps) on the way in.

Identifiers are masked exactly, never heuristically: the redactor masks our own
32-character hex account digest, so applying the heuristic to `account_id` or a
window id would turn every account into the same `[redacted]` and merge unrelated
buckets. That boundary is exactly 32 characters — the real `derive_account_id`
output — which `tests/test_secret_masking.py` uses rather than a longer string
that would not exercise it.

Review then found two provider-supplied fields still open, `observed_at` and
`account_id`, both of which now go through the choke point as well.

Re-run end to end through the running service — the stand-in echoing the key from
every field it controls, the worker polling it, and the API serving the result:

```
canary in /api/v1/usage response: False
canary in stored observations:    False
masking present:                  True
```

The value is masked rather than dropped, so the field still reports that the
provider sent something while withholding what it was.

Still outstanding for 5.2 and 5.4, all needing the host:

- installing the unit and confirming it starts under systemd;
- a `systemctl restart` (the check above restarts the process, not the unit);
- the journal redaction check, since `journalctl` only knows installed units;
- verifying the upgrade, rollback and rotation procedures against the deployed
  layout once it exists.

## Tailscale Serve readiness

| Item | Value |
| --- | --- |
| Serve available | Yes (Tailscale 1.102.4 includes Serve) |
| Current HTTPS publications | Four mappings, all `(tailnet only)`: `3773`, `8443`, `8444`, `8445`; port `443` is free |
| Funnel | Off — `tailscale funnel status` reports every mapping as `(tailnet only)` |
| HTTPS certificates | Enabled in the tailnet (the existing mappings already use `ts.net` names) |
| Node and domain | `zoey` (`100.108.85.109`), `zoey.tail831c33.ts.net` |
| Intended clients present on the tailnet | `jamess-macbook-pro` (macOS), `iphone182-2` (iOS) |
| Denied-device candidates | Tagged service nodes — `rumi`, `happier`, `mira`, `ranger`, `grafana`, `livesync` — which are not granted access to user devices |
| Plan | Publish the service port (bound to `127.0.0.1`) via `tailscale serve`, restrict with tailnet identity/device rules for the intended Mac and iPhone, Funnel stays disabled |

Serve access control is the tailnet policy file, which is deny-by-default and not
visible from the CLI, so the access rules themselves remain unverified. The
survey, `tailscale serve status`, and `tailscale funnel status` all ran without
privileges; `tailscale serve` itself does not.

## Verification results (local, 2026-10-19)

Both tools run against this host's real providers; neither reads credentials.

| Check | Tool | Result |
| --- | --- | --- |
| HTTP contract, loopback-only binding | `tools/smoke_check.py` | Pass — 4 source IDs in `/health`, `observations: []` before first success, `Cache-Control: private, no-store`, non-loopback address refused, no credential-looking text in responses |
| API latency independent of provider polls | `tools/soak_check.py` (150 s) | Pass — median 1.0 ms, p95 1.8 ms, max 14.2 ms across 450 requests |
| First-failure omission | `tools/soak_check.py` | Pass — usage observation counts went 0 → 1 and never exposed a never-successful source |
| Independent per-source failure | `tools/soak_check.py` | Pass — `codex` healthy while `antigravity` reported `unavailable` and both OpenRouter sources reported `operator_action` |
| Bounded history | `tools/soak_check.py` | Pass — `hours` present, `truncated` reported |
| Codex collection through stdio | `tools/soak_check.py`, `tools/codex_live_check.py` | Pass — real account data, no collection failures over 150 s |
| Soak across an observed reset | `tools/reset_watch.py` (50 min) | Pass — the Codex 5h window turned over at 2026-10-19T19:37:35Z; the next sample reported `usedPercent: 0.0` with a new `resetsAt` and pace recomputed to the **new** window (`expectedUsedPercent: 0.0`), not the window that just ended (which would read ~100) |
| Pace refuses a clock-tracking boundary | `tools/reset_watch.py` (4 min) | Pass — after the reset the app-server reported `resetsAt` as `now + 5h` on every poll (~61 s per 60 s poll), and pace is now `unknown` instead of a fabricated `at` |
| Partial OpenRouter results | `tools/openrouter_live_check.py` | Pass — with a management key and no inference key, `openrouter-account` collected real credits while `openrouter-key` reported `not_configured`, each with its own health; absent credit fields stayed `null` |

### Finding: a window end that tracks the clock

The reset soak exposed a real defect. Before the reset the window end was a fixed
instant and the elapsed fraction climbed 96% → 99.96%, which is the model pace
assumes. After the reset the app-server reported `resetsAt` as `now + 5h` on
every poll until something consumed the new window. The inferred start is then
always "now", so the elapsed fraction is permanently zero and the service
reported a confident `{"classification": "at", "expectedUsedPercent": 0.0}`.

That label is fabricated, and the spec requires `unknown` when reset timing is
implausible. Had the account been in use, the same drift would have produced a
permanent false `over` instead. Fixed in commit `2577656`; the check compares the
most recent prior snapshot only, since the rule is about consecutive snapshots.

Every check in this table now has live evidence. The one qualification: the
current-key stream's *success* path was exercised with an invalid key
(`authentication`) and by unit tests, not with a real inference key, because
only a management key is available on this host. `tools/openrouter_live_check.py`
runs both streams and reports each one's status and error category without
printing the credential; it exits `2` when no credential is present.

## Widget response sizes against the configured cap

Task 5.3 also asks for confirmation that widget response sizes stay within the
configured cap. Two different measurements, with different fixtures — the numbers
are only meaningful with the fixture attached.

**Real adapters on this host**, fresh data directory, one polling cycle
(`python3 tools/smoke_check.py`; the totals move by a byte or two between runs
because provider values change, but the fixture no longer does):

| Endpoint | Bytes |
| --- | --- |
| `/api/v1/version` | 48 |
| `/health` | 833 |
| `/api/v1/usage` | 929 |
| `/api/v1/usage/history?hours=1&limit=5` | 961 |

Only `codex` succeeds on this host, so that table covers one source, not four.

**Worst case, seeded**: four sources with 200 stored observations each, written
directly to a database by a throwaway script rather than by the committed tools —
`antigravity` and the OpenRouter sources cannot succeed here, so this shape
cannot be produced by polling this host. Reproduce by seeding a database and
calling `history_payload` / `usage_payload`.

| Request | Bytes | Outcome |
| --- | --- | --- |
| `/api/v1/usage` | 3201 | served |
| `/api/v1/usage/history?hours=168&limit=100` | 78042 | served |
| `/api/v1/usage/history?hours=168&limit=250` | 194371 | served |
| `/api/v1/usage/history?hours=168&limit=500` | 261945 | served — 336 rows, `truncated: true` |

The last row is the interesting one. The maximum configured `limit` of 500 does
not fit under the 262144-byte cap with four sources populated, so the endpoint
drops the oldest rows until it fits and reports `truncated`. It degrades instead
of failing, and the newest reading is always kept. That invariant is what
`tests/test_service.py` pins (byte cap and `truncated`); the exact row count is a
property of the fixture, not a guarantee.

Both the truncation check and the HTTP layer measure the same bytes —
`json.dumps(...).encode("utf-8")` — and `json.dumps` escapes non-ASCII by
default, so a label containing `·` cannot make the body that is sent larger than
the body that was measured.

One cliff is worth knowing. `/api/v1/usage` carries every source in a single
response and has no truncation, so enough windows in one source push it over the
cap and the endpoint returns 413 — the widget then gets nothing, including from
the healthy sources. On the seeded fixture that boundary is 774 windows, which is
a function of window width (~338 bytes each) rather than a fixed number; a
narrower window moves it. Real providers report single digits, the spec documents
413 for an oversized body, and bounding the window count is a design decision
rather than a bug fix, so this is recorded rather than changed.

## Capability summary for adapter enablement

| Source | Enable now? | Notes |
| --- | --- | --- |
| `codex` | Yes | Default `codex_transport = "stdio"` needs no daemon (verified live). A healthy app-server daemon socket also works via `codex_transport = "socket"`. Protocol and pace math are verified live |
| `antigravity` | No (session-only variant) | Adapter enabled but expected to report `unavailable`; no weekly quota to collect |
| `openrouter-account` | No (no credential) | Awaiting a management/provisioning credential for `/api/v1/credits` (`openrouter_management_key`, falling back to `openrouter_api_key`); error category `not_configured` until set, `authorization` if a non-management key is used |
| `openrouter-key` | No (no credential) | Awaiting credential for `/api/v1/key`; error category `not_configured` until set |
