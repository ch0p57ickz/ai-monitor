## 1. Host validation and project setup

- [x] 1.1 Produce a redacted GX10 capability report (report: `docs/gx10-capability-report.md`) for Python, the service manager, Codex app-server, Antigravity, both OpenRouter endpoints and their credential permissions, and Tailscale; verify it lists versions, bucket names, field presence, and separate endpoint error categories without secrets or raw payloads.
- [x] 1.2 Create the Python service package, configuration loading, and documented default poll intervals and bounds; verify the service starts locally with unavailable adapters and rejects invalid configuration.

## 2. Data model, persistence, and pace

- [x] 2.1 Define normalized observations, quota windows, budget records, opaque account IDs, and the version 1 JSON fields and enums in the API spec; verify synthetic serialization matches the contract, missing fields stay `null`, and account and key budgets remain distinct.
- [x] 2.2 Implement SQLite schema, WAL setup, short writes, latest-observation reads per source, and bounded retention; verify a restart preserves successful observations and pruning keeps each source's latest result.
- [ ] 2.3 Implement source state and freshness from last attempt, last success, and configured intervals; verify failed polls retain the last good reading, stale begins after two missed intervals, and a never-successful source has no usage observation but has health state.
- [ ] 2.4 Implement quota pace with reported or estimated window starts, plausibility checks, and configurable tolerance; verify synthetic cases for under, at, over, estimated, and unknown pace.
- [ ] 2.5 Implement same-account OpenRouter spend deltas and exact-boundary budget pace; verify account changes and `limit_reset: "monthly"` without a timestamp do not produce an exact pace.

## 3. Provider adapters and scheduling

- [ ] 3.1 Implement the Codex app-server handshake and rate-limit parser; verify synthetic keyed limits preserve primary and secondary windows and the single-view fallback works.
- [ ] 3.2 Implement Antigravity source discovery and supported-bucket parsing from the validated GX10 variant; verify session-only data is retained without a fabricated weekly quota and incompatible sources report unavailable.
- [ ] 3.3 Implement independently committed OpenRouter account-credit and current-key results using the permitted credentials; verify either endpoint can succeed alone, each has separate health and freshness, and absent credit fields stay unknown.
- [ ] 3.4 Implement independent scheduled workers with deadlines, no same-source overlap, capped local backoff, jitter, `Retry-After` as an uncapped minimum delay, and operator-action handling for 401/403; verify a long `Retry-After` is honored and a failing or hung source does not delay another.
- [ ] 3.5 Add redacted diagnostics and parser fixtures for missing fields, changed shapes, and source errors; verify logs and persisted records contain no credentials, CSRF tokens, email addresses, or full payloads.

## 4. Cached HTTP API

- [ ] 4.1 Implement `GET /api/v1/usage` from committed observations only; verify it returns `observations: []` before first success, omits never-successful sources, retains prior successes during failures, and never starts a poll.
- [ ] 4.2 Implement bounded, time-ordered `GET /api/v1/usage/history` with a 24-hour default, 7-day maximum, 100-result default, 500-result maximum, and `truncated`; verify contract fixtures for invalid, oversized, and partially returned requests.
- [ ] 4.3 Implement `GET /health` for all four source IDs and shared version 1 JSON/error serialization with UTC timestamps, defined enums, private cache headers, and a 256 KiB response cap; verify contract fixtures for partial OpenRouter access and safe error bodies.
- [ ] 4.4 Bind the API only to `127.0.0.1` and expose only the documented read-only routes; verify direct requests to the host's non-loopback address cannot reach the port.

## 5. GX10 deployment and acceptance

- [ ] 5.1 Run local smoke checks and a GX10 soak period across an observed reset or representative shorter interval; verify first-failure omission, partial OpenRouter results, correct freshness, bounded history, independent failures, and API latency independent of provider polls.
- [ ] 5.2 Install the service under a dedicated unprivileged account with persistent storage, credential access, and automatic restart; verify a restart preserves cached data and no secret appears in logs or responses.
- [ ] 5.3 Configure Tailscale Serve HTTPS and tailnet identity/device rules without Funnel; verify access from the intended Mac and iPhone and denial from a disallowed device, and confirm widget response sizes remain within the configured cap.
- [ ] 5.4 Document GX10-specific start, upgrade, backup, rollback, and credential-rotation commands; verify each command or procedure against the deployed host configuration.
