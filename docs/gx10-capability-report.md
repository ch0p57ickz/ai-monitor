# GX10 Capability Report (redacted)

Date: 2026-10-19
Verified on: `zoey` (100.108.85.109), Tailnet `papaya.ch0mp@`
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
| Control socket | `~/.codex/app-server-control/app-server-control.sock` exists while `codex app-server daemon` runs. On this host the managed daemon accepts then closes every connection, including `codex app-server proxy` (broken pipe); a directly started `codex app-server --listen stdio://` works. The daemon logs `Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces` |
| Auth state | `~/.codex/auth.json` present (contents not read, not reported) |
| Protocol | Confirmed from `codex app-server generate-json-schema` and a live session: the connection must send `initialize` first (any other request is answered with a closed connection), then `account/rateLimits/read` (`Account/rateLimits/readRequest`, response `GetAccountRateLimitsResponse`). Server notifications (`configWarning`, `account/updated`) arrive between replies and must be skipped |
| Rate-limit fields | `rateLimitsByLimitId` (keyed) with a `rateLimits` single-view mirror; each snapshot has `primary`/`secondary` `RateLimitWindow` objects carrying `usedPercent` (integer), `windowDurationMins`, and `resetsAt` (Unix seconds). `accountId` is defined but was absent in the live payload |

Live evidence (`tools/codex_live_check.py`, bridged stdio app-server, real
account, snapshot 2026-10-19): `view: keyed`, two windows — `5h · primary`
at 14% used against `expectedUsedPercent` 69.45 → `under`, and `7d ·
secondary` at 23% used against `expectedUsedPercent` 27.03 → `at`;
`accountId` was null in the live payload. Percentages move with use; the
classification and the presence of real reset bounds are the point.

Verdict: capable. The adapter protocol is verified against the live app-server;
on this host it must be reached through a working app-server transport because
the managed daemon socket is unhealthy (the GX10 install step should confirm
`codex app-server daemon start` produces a usable socket).

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
categories. The account-credit endpoint requires a credential and is listed for
completeness only. No OpenRouter credential is configured on this host yet:
`envget -s` (name-only listing) shows no OpenRouter credential variable, and the
process environment contains no OpenRouter key.

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

The soak across an observed reset, the live OpenRouter credential check, and the
Tailscale access rules remain pending (tasks 5.1-5.3).

## Tailscale Serve readiness

| Item | Value |
| --- | --- |
| Serve available | Yes (Tailscale 1.102.4 includes Serve) |
| Current HTTPS publications | Not verified (requires `tailscale serve status`) |
| Plan | Publish the service port (bound to `127.0.0.1`) via `tailscale serve`, restrict with tailnet identity/device rules for the intended Mac and iPhone, Funnel stays disabled |

## Capability summary for adapter enablement

| Source | Enable now? | Notes |
| --- | --- | --- |
| `codex` | Yes (after daemon start) | Start `codex app-server daemon` on the service account and confirm the socket answers (see the Codex section); the adapter protocol and pace math are verified live |
| `antigravity` | No (session-only variant) | Adapter enabled but expected to report `unavailable`; no weekly quota to collect |
| `openrouter-account` | No (no credential) | Awaiting a management/provisioning credential for `/api/v1/credits` (`openrouter_management_key`, falling back to `openrouter_api_key`); error category `not_configured` until set, `authorization` if a non-management key is used |
| `openrouter-key` | No (no credential) | Awaiting credential for `/api/v1/key`; error category `not_configured` until set |
