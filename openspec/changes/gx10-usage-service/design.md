## Context

See [proposal.md](proposal.md) for motivation and the four delta specs for behavior. This repository currently has OpenSpec configuration but no service implementation or existing capability specs. The GX10 runtime, signed-in local tools, credential access, and Tailscale state have not been verified from this workspace. The Antigravity interface is version-sensitive and may expose only session data.

## Goals / Non-Goals

**Goals:**

- Keep collector failures and deadlines isolated from the HTTP request path.
- Store enough normalized data for bounded history and cautious pace calculations without storing raw provider responses.
- Make unsupported provider fields and host capabilities visible as unknown or unavailable.
- Allow a GX10 operator to deploy, roll back, and rotate credentials after validating the host.

**Non-Goals:**

- Building the macOS or iOS widgets in this change.
- Writing to provider accounts or changing provider quotas.
- Treating a local Antigravity interface as a stable public API or guaranteeing weekly data.
- Exposing the service through the public internet.

## Decisions

### 1. Validate GX10 capabilities before enabling adapters

Begin implementation with a redacted host report that records installed versions, endpoint success or error category, bucket names, field presence, and Tailscale readiness. It must never print credentials or full responses. Check OpenRouter account-credit and current-key access separately. Use this report to enable each working source; a missing Antigravity source remains disabled and visible as `unavailable` in health.

This avoids designing against unverified local interfaces. The alternative of assuming all sources exist would turn a host mismatch into a deployment failure.

### 2. Build one small Python service with isolated adapter workers

Use Python 3 and its standard library where practical: local subprocess or HTTP clients, `sqlite3`, a bounded HTTP server, and a scheduler with one in-flight attempt per source. Verify the Python version and service manager on GX10 during the host report. Keep three provider adapters, with the OpenRouter adapter producing independently tracked account-credit and current-key streams. The API reads only committed data. Configure polling intervals and deadlines, with defaults from the source spec. A failed stream schedules capped exponential backoff with jitter. For a valid `Retry-After`, schedule no earlier than the later of that delay and local backoff; the local cap does not shorten the provider's delay. HTTP 401/403 enter an operator-action state.

This keeps deployment dependencies small and parsing separate from serving. A framework-based service would add runtime dependencies without improving the three read-only routes.

### 3. Normalize before persisting

Adapters convert provider payloads into quota windows and budget records, with absent values represented as `null`. Codex uses keyed limits first and preserves primary and secondary windows. Antigravity preserves reported group/model/scope only. OpenRouter account credits and key limits remain separate: commit either successful endpoint result without waiting for the other. USD values use decimal-safe representation. Derive opaque account IDs from source identity using a local keyed digest; do not store source account names or email addresses. If no safe identity is available, use `null` and avoid account-dependent trends.

The alternative of storing raw payloads would simplify parser debugging but would retain sensitive and unstable fields.

### 4. Persist successful snapshots and calculate freshness independently

Store successful normalized observations in SQLite on persistent storage, using WAL and short transactions. Keep the latest result for each source and time-ordered history with default retention of 30 days and an adjustable count cap. Store last attempt, last success, and error category per source so a failed attempt never replaces a good observation. Compute `staleAfter` as last success plus two configured intervals. Before a source's first success, return no usage observation for it; `/health` still reports its state and error. Detect account changes and prevent cross-account trend calculations.

SQLite provides durable local history without another service. In-memory-only caching would lose history and freshness context on restart.

### 5. Keep pace classification explicit about evidence

Represent pace as a classification (`under`, `at`, `over`, or `unknown`) plus a basis (`reported`, `estimated`, or `none`) and optional expected-use percentage. Use the elapsed-window fraction and a default five-percentage-point tolerance. Infer a start from reset minus duration only when the reset is plausible, and mark that basis `estimated`. OpenRouter key budget pace requires an exact period boundary; a `monthly` label alone is insufficient. Recent spend may use consecutive same-account snapshots; an optional projection must be labeled and requires enough history to be meaningful.

This conveys the difference between measured and inferred timing. A single unqualified pace label would overstate precision.

### 6. Serve a bounded versioned contract from cache

Use the exact JSON fields and enums in the widget API spec, with `schemaVersion: 1` and UTC ISO 8601 timestamps. `/api/v1/usage` returns an empty `observations` array until a source succeeds, then includes only each source's latest committed result. `/api/v1/usage/history` uses a 24-hour default, 7-day maximum, and 100-result default with a 500-result maximum. Select the most recent matching records under the count and byte bounds, return them in ascending collection-time order, and set `truncated` when any matching records were omitted. `/health` reports all four source states, including absent or failing sources. Reject invalid query values with stable error categories. Apply a 256 KiB response cap and private cache headers appropriate for the poll interval. The HTTP handler must not call adapters.

This makes widget latency independent of provider latency. Polling on request would couple the API to source outages and deadlines.

### 7. Separate local binding from tailnet publication

Bind the service to `127.0.0.1` and publish that port with Tailscale Serve over HTTPS after checking the host's actual Tailscale configuration. Verify tailnet identity and device rules with an allowed and a disallowed device; keep Funnel disabled. Use a dedicated unprivileged service account, a persistent data directory, automatic restart, and the account's supported credential mechanism. Log only error categories and non-sensitive operational metadata.

This limits the service's network surface and secret exposure. Binding to all interfaces would bypass the intended Serve access boundary.

## Risks / Trade-offs

- **Antigravity changes or disappears** → Keep its adapter optional, report `unavailable`, and validate only observed bucket fields.
- **Codex app-server or OpenRouter responses change** → Isolate parsers and cover missing fields, alternate windows, and error shapes with synthetic fixtures.
- **Estimated pace misleads users on rolling quotas** → Mark estimates, reject implausible resets, and return `unknown` when evidence is insufficient.
- **SQLite growth or slow widget responses** → Enforce retention, query limits, and response byte bounds.
- **Credential or account data leaks through diagnostics** → Use redacted capability reports, structured error categories, and opaque account IDs.
- **Host platform differs from assumptions** → Validate Python, service manager, local endpoints, and Tailscale before choosing exact deployment commands.

## Migration Plan

1. Collect the redacted GX10 capability report and confirm runtime, credentials, and Tailscale controls.
2. Install the service under a dedicated account with persistent SQLite storage; run local smoke checks and a soak period spanning an observed reset or representative shorter interval.
3. Configure Tailscale Serve and access rules, then verify HTTPS responses and denial from a disallowed device.
4. Document host-specific start, upgrade, backup, and credential rotation commands. For rollback, stop Serve publication, restore the previous service version and compatible database backup, then restart the service.
