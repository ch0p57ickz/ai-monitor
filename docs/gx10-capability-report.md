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
| Denied-device candidates | Tagged service nodes — `rumi`, `happier`, `mira`, `ranger`, `grafana`, `livesync`, `hermes`, `openclaw-gx10` — all `tag:agent`, owned by the `tagged-devices` identity rather than the operator |
| Node ownership | User-owned, untagged (`tailscale status --json` shows no `Tags`, no advertised services) |
| Service alternative | Viable and lower blast radius: Service access needs an explicit `svc:` grant, so the allow-all node policy is untouched. Requires tagging `zoey` (a Service host must be tag-based) and advertising the Service from `zoey` itself |
| Plan | Publish the service port (bound to `127.0.0.1`) via `tailscale serve`, restrict with tailnet identity/device rules for the intended Mac and iPhone, Funnel stays disabled |

### Finding: the policy is allow-all, and its effect is observable

The policy file is not readable from the CLI, but its effect on this node is.
`tailscale debug netmap` runs unprivileged and carries the packet filter the
control plane distributed to `zoey`. On 2026-10-19 it held a single rule:

```text
SrcIPs   100.115.94.0-100.127.255.255, 100.64.0.0-100.115.91.255, fd7a:115c:a1e0::/48
DstPorts IP "*", ports 0-65535
```

That is the translated form of the default allow-all policy, so every member
device reaches `zoey` on every port today, `tag:agent` service nodes included. The
earlier note that the access rules "remain unverified" was too pessimistic: the
rules are not readable, but the filter they produce is.

Two consequences:

1. **Adding a rule cannot restrict anything.** Rules union and the only action is
   `accept`, so the allow-all rule has to be replaced by explicit grants. A
   destination cannot be negated, so the served port has to be excluded from the
   broad rule — globally, not just on `zoey`. If another node serves HTTPS on
   `443` it then needs a grant of its own; `9443` is free on `zoey` and makes the
   exclusion harmless, which is the alternative if that check cannot be made.
2. **The change is verifiable without a console session.** Tailscale's policy
   `tests` section asserts accept and deny per source and destination, and the
   console runs it on save. `deploy/tailnet-policy.example.hujson` carries a
   `tests` block asserting the denial for `tag:agent` on `443` *and* that the same
   identity keeps reaching `8443`. `tests/test_tailnet_policy.py` holds the
   example to the same requirement offline and fails if the blanket rule is edited
   back to covering `443`.

The device-level check still needs a device: from a `tag:agent` node, `443` should
time out while `8443` still answers.

A Service avoids the port-exclusion collateral altogether, because Service access
is granted by a named `svc:` rule rather than by the node rules. The cost is that a
Service host must be tag-based, so `zoey` has to be tagged, and the Service has to
be advertised from `zoey` since the API binds loopback only. Checked against the
docs rather than assumed: identity headers are filled from the *requester's*
identity, so tagging the host does not strip them from the four existing routes;
`RunSSH: false` on `zoey`, so no Tailscale SSH session is at risk; but
`autogroup:self` and user-based selectors stop matching a tagged `zoey`, and
tagging re-registers the node. The runbook has the ordered steps.

### Finding: adding a `grants` section can flip the tailnet to deny-by-default

Service access needs a `grants` entry, and the documentation says adding one is
harmless, because the default allow-all policy applies "in the absence of an `acls`
section". Tailscale's issue tracker contradicts that: the default policy is filled in
only when the file is blanked *entirely*, not section by section, and "the absence of
any allow rules enables global default-allow" ([tailscale#2799][issue-2799]). Read
that way, adding a `grants` section to a file with no `acls` section removes the only
allow rule and cuts every device-to-device connection — `zoey`'s four Serve routes
included.

The console answers the question without an edit: **Convert to grants** is enabled
only when the policy file contains an `acls` section. Enabled means an explicit allow
rule exists and the grant is additive. Disabled means the ambiguous case, and the
answer is to spell the allow-all rule out in the same edit, so the result stops
depending on how an absent section is interpreted.

[issue-2799]: https://github.com/tailscale/tailscale/issues/2799

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

## Packaging verified without privileges (tasks 5.2 and 5.4)

Three runbook procedures — install, upgrade, and rollback — run
`pip install /opt/ai-monitor` in the service account's venv, and none had ever
been executed. They can be, without sudo: build a venv in a temporary directory,
install from a copy of the checkout, and run the console script the unit runs.

| Step | Result |
| --- | --- |
| `pip install <checkout>` (index reachable) | Pass — builds a wheel and installs `ai-monitor` 0.1.0 |
| `ai-monitor --version` | Pass — `ai-monitor 0.1.0` |
| Service from the installed script | Pass — serves `/api/v1/version` and `/health`, shuts down cleanly on SIGTERM |
| Upgrade (package as 0.2.0, reinstall) | Pass — all three version sources report 0.2.0 |
| Rollback (back to 0.1.0, reinstall) | Pass — all three report 0.1.0 |
| Offline: `pip wheel` then install the wheel into a clean venv | Pass — installs and runs with no index and no setuptools on the target |

What is *not* covered: systemd starting the unit, the unit's sandbox, and the
service account's own provider sign-in. Those need the host.

### Finding: the install needs network, or a prebuilt wheel

`pip install <checkout>` uses build isolation, which installs `setuptools>=68`
into a temporary build environment. A Python 3.12 venv has no setuptools of its
own, so with no index the install fails:

```
pip subprocess to install build dependencies did not run successfully
```

A pre-warmed pip cache does not rescue it (verified). The offline answer is a
prebuilt wheel: build it where setuptools is available, then install it. Installing
a wheel builds nothing, so the target needs neither setuptools nor an index —
verified by building `ai_monitor-0.1.0-py3-none-any.whl` and installing it into a
clean venv that has no setuptools at all.

`--system-site-packages` looks like a simpler offline fix and is a trap. It puts
the system site-packages on the venv's path, so pip treats a system-wide
`ai-monitor` as already installed and can report success while leaving the old
version in place — precisely the silent failed upgrade this section is about. The
runbook uses the wheel form and warns against the venv flag.

### Finding: the install command copied `.env` into the deployment tree

Rehearsing the copy step against a scratch destination — not reading the file, just
checking what lands — showed `rsync -a` of the checkout brings `.env` along. In
this repository `.env` is mode **0664**, and `/opt/ai-monitor` is **0755**, so the
operator's credentials would sit world-readable on the deployed host. The service
never reads that file; the credentials it uses live in `/etc/ai-monitor/config.json`
at `0640 root:ai-monitor`.

The step now names what to copy instead of what to skip:

```sh
sudo rsync -a --delete \
  --include '/ai_monitor/***' --include '/deploy/***' --include '/docs/***' \
  --include '/tools/***' --include '/pyproject.toml' \
  --exclude '*' \
  /path/to/this/checkout/ /opt/ai-monitor/
```

Rehearsed: the destination then holds exactly `ai_monitor/`, `deploy/`, `docs/`,
`tools/`, `pyproject.toml`, and the pre-existing `venv/`. `.env` and `.gitignore`
do not arrive, a module deleted from the package does not survive, and the venv
does, because rsync treats excluded paths as protected from `--delete`.

The allowlist is deliberate. A denylist fails open — the file that matters is the
one nobody thought to exclude. An allowlist fails safe: its failure mode is a
missing file, which is loud, rather than a disclosed secret, which is silent.
`tests/test_runbook_install.py` holds both copy steps (install and upgrade) to that
shape, and each step now asserts `test ! -e /opt/ai-monitor/.env` afterwards.

### Finding: the reported version had two sources, and they drifted

`__version__` was hardcoded in `ai_monitor/__init__.py` while `pyproject.toml`
carried the packaged version, and nothing compared them. Packaging 0.2.0 over
0.1.0 showed the consequence:

| Source | Reported |
| --- | --- |
| `pip show ai-monitor` | 0.2.0 |
| `ai-monitor --version` | 0.1.0 |
| `/api/v1/version` → `service_version` | 0.1.0 |

An upgrade would therefore look like it had not taken effect, and the API would
serve a stale `service_version`. `__version__` now reads the installed
distribution metadata, falling back to a `SOURCE_VERSION` constant when running
from a checkout, and a test pins that constant against `pyproject.toml` so the two
cannot drift silently again. Re-verified end to end: install, upgrade, and
rollback each report one consistent version across all three sources.

## First deployment attempt: 5.2 is not complete (verified 2026-10-19)

The install was attempted on this host and does not work. Recording the evidence
because 5.2 is exactly the task that asks for this verification, and because
`systemctl enable --now` reported success.

| Check | Result |
| --- | --- |
| Service account | Present (`ai-monitor`, uid 996) |
| State directory | Present, `/var/lib/ai-monitor`, `0700 ai-monitor` |
| Config file | Present and not readable by the operator (`/etc/ai-monitor/config.json`) — correct for `0640 root:ai-monitor` |
| Unit installed | Present at `/etc/systemd/system/ai-monitor.service` |
| **Unit matches the repository** | **No** — the installed unit predates `ReadWritePaths=/home/ai-monitor` |
| **`/opt/ai-monitor`** | **Empty — no checkout, no venv, no binary** |
| Unit state | `activating (auto-restart)`, `status=203/EXEC`, restart counter **2141** |
| API on `127.0.0.1:8732` | Not listening |

Two independent problems:

1. **The code and virtualenv were never installed.** `/opt/ai-monitor` is empty,
   so `ExecStart=/opt/ai-monitor/venv/bin/ai-monitor` cannot be executed and the
   unit exits `203/EXEC`. The runbook's step 2 was skipped, and its command was
   part of why: it said `git clone <repo-url>`, and this repository has **no
   remote**, so the command cannot be run as written.
2. **The installed unit is stale.** It lacks `ReadWritePaths=/home/ai-monitor`,
   the line that keeps the service account's home writable under
   `ProtectSystem=strict`. Even with the code installed, the bundled Codex CLI
   would fail to write `~/.codex`, which is the failure recorded in the reset-soak
   finding.

The failure was invisible for three reasons, all now addressed:

- `systemctl enable --now` returns success when the unit fails immediately, and
  `systemctl is-enabled` still reads `enabled` while it crash-loops. The runbook
  now checks `systemctl is-active` and reads the journal after starting.
- Nothing named the missing binary. `systemd-analyze verify` does — it prints
  "Command /opt/ai-monitor/venv/bin/ai-monitor is not executable" — so the runbook
  now runs it before starting the unit.
- `Restart=always` retried forever with no limit, so 2141 failed starts accumulated
  without the unit ever reporting `failed`. The unit now sets
  `StartLimitIntervalSec=300` / `StartLimitBurst=10`, turning that loop into a
  reported failure. A start limit does not conflict with the spec's automatic
  restart: the service still restarts itself, it just stops retrying a start that
  cannot succeed.

Remediation, in order: install the current unit, install the code and venv, then
`systemctl reset-failed ai-monitor` and start. The runbook's install section has
the commands.

## Second deployment attempt: the unit works, the config is missing (2026-10-19)

The install went further. The unit is now the current one (`diff` against the
repository is clean), the virtualenv installs and runs — `ai-monitor --version`
reports `0.1.0` — and the failure moved on:

```text
ai-monitor[3043009]: ERROR ai_monitor: config error: config file not found:
                     /etc/ai-monitor/config.json
systemd[1]: Main process exited, code=exited, status=2/INVALIDARGUMENT
systemd[1]: Start request repeated too quickly.
systemd[1]: Failed to start ai-monitor.service
```

`/etc/ai-monitor` exists at `0750 root:ai-monitor`; the config file step was
skipped. This is the third distinct silent failure in the same install, and the
first the start limit caught: `StartLimitBurst` stopped it after ten attempts and
reported `failed`, where the earlier unit restarted indefinitely. That change is
what turned an invisible loop into a named failure.

**The deployment tree carries a world-readable `.env`.** `/opt/ai-monitor` is
`0775`, `/opt/ai-monitor/.env` is `0664 ai-monitor:ai-monitor`, and `/opt` is
world-traversable, so any local user can read it. The copy was a full-tree copy
rather than the runbook's allowlist form. The service never reads that file — it
reads `/etc/ai-monitor/config.json` — so it is pure liability: until it is removed,
treat its contents as disclosed and rotate them.

The tree also carries `build/`, `ai_monitor.egg-info/`, `tests/`, `openspec/`,
`.opencode/`, `.agents/`, `.pytest_cache/`, and `.gitignore`, none of which belong
in a deployment. The allowlist step exists to keep them out.

Three attempts have now failed on three different preconditions, each invisible
until the journal was read, so the runbook has one precondition block before
`enable --now` with a line per failure this host has produced. The config check runs
as the service account deliberately: `sudo test -r` runs as root and passes no matter
what the service will actually see.

## Capability summary for adapter enablement

| Source | Enable now? | Notes |
| --- | --- | --- |
| `codex` | Yes | Default `codex_transport = "stdio"` needs no daemon (verified live). A healthy app-server daemon socket also works via `codex_transport = "socket"`. Protocol and pace math are verified live |
| `antigravity` | No (session-only variant) | Adapter enabled but expected to report `unavailable`; no weekly quota to collect |
| `openrouter-account` | No (no credential) | Awaiting a management/provisioning credential for `/api/v1/credits` (`openrouter_management_key`, falling back to `openrouter_api_key`); error category `not_configured` until set, `authorization` if a non-management key is used |
| `openrouter-key` | No (no credential) | Awaiting credential for `/api/v1/key`; error category `not_configured` until set |
