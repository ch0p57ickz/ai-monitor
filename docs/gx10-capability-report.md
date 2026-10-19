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
| App-server subcommand | `codex app-server` exists (experimental): `daemon`, `proxy`, `generate-ts`, `generate-json-schema` |
| Control socket | `~/.codex/app-server-control/app-server-control.sock` — not present at check time; the daemon is not running and must be started (`codex app-server daemon start`) before rate-limit collection can be exercised |
| Auth state | `~/.codex/auth.json` present (contents not read, not reported) |
| Rate-limit fields | Not verified yet: the parser contract from the delta spec (`rateLimitsByLimitId` keyed view with primary/secondary windows; `rateLimits` single-view fallback) will be exercised by the adapter against the live app-server once it is running (task 3.1) |

Verdict: capable, daemon start is a prerequisite.

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

## Tailscale Serve readiness

| Item | Value |
| --- | --- |
| Serve available | Yes (Tailscale 1.102.4 includes Serve) |
| Current HTTPS publications | Not verified (requires `tailscale serve status`) |
| Plan | Publish the service port (bound to `127.0.0.1`) via `tailscale serve`, restrict with tailnet identity/device rules for the intended Mac and iPhone, Funnel stays disabled |

## Capability summary for adapter enablement

| Source | Enable now? | Notes |
| --- | --- | --- |
| `codex` | Yes (after daemon start) | Start `codex app-server daemon` on the service account; validate the keyed rate-limit view at first poll |
| `antigravity` | No (session-only variant) | Adapter enabled but expected to report `unavailable`; no weekly quota to collect |
| `openrouter-account` | No (no credential) | Awaiting a management/provisioning credential for `/api/v1/credits` (`openrouter_management_key`, falling back to `openrouter_api_key`); error category `not_configured` until set, `authorization` if a non-management key is used |
| `openrouter-key` | No (no credential) | Awaiting credential for `/api/v1/key`; error category `not_configured` until set |
