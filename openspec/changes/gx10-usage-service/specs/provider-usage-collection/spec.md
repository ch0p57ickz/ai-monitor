## Purpose

Collect usage from Codex, Antigravity, and OpenRouter independently so a failure or unsupported interface in one source does not hide the others.

## ADDED Requirements

### Requirement: Independent provider collection
The service SHALL poll each enabled source on its own configurable schedule, SHALL bound each attempt by a deadline, and SHALL prevent overlapping attempts for the same source. OpenRouter account credits and current-key usage SHALL count as separate sources within one provider adapter. The initial intervals SHALL be 60 seconds for Codex and Antigravity and 5 minutes for each OpenRouter source.

#### Scenario: One provider fails
- **WHEN** a provider poll fails or reaches its deadline
- **THEN** the other provider schedules continue and their successful observations remain available

#### Scenario: Poll remains active
- **WHEN** a source's next scheduled poll is due while its previous poll is still active
- **THEN** the service does not start an overlapping poll for that source

### Requirement: Bounded retry behavior
The service SHALL retry transient failures with capped exponential backoff and jitter. A valid `Retry-After` SHALL set a minimum delay before the next attempt, even when it exceeds the local backoff cap. The service SHALL classify authentication or authorization failures as requiring operator action without aggressive retries.

#### Scenario: Provider rate limit
- **WHEN** a provider responds with a retry delay
- **THEN** the next attempt starts no earlier than the supplied delay, even if that delay exceeds the local backoff cap

#### Scenario: Invalid credentials
- **WHEN** a provider responds with HTTP 401 or 403
- **THEN** its state records an authentication or authorization error and does not enter a rapid retry loop

### Requirement: Codex rate limit collection
The Codex adapter SHALL use the locally available app-server rate limit interface and preserve each reported limit ID and its primary and secondary windows as distinct quota windows. It SHALL use the single `rateLimits` view only when the keyed view is absent, and SHALL assign duration-based labels only when the reported duration supports them.

#### Scenario: Multiple Codex limits
- **WHEN** `rateLimitsByLimitId` contains multiple limits with primary and secondary windows
- **THEN** the observation contains separately identified windows for every reported limit and window type

#### Scenario: Keyed view unavailable
- **WHEN** the Codex response lacks `rateLimitsByLimitId` but includes `rateLimits`
- **THEN** the adapter collects the available single-view windows without inventing limit IDs

### Requirement: Antigravity capability-aware collection
The Antigravity adapter SHALL report only quota buckets and scopes supported by the installed local variant. It SHALL retain reported group, model, and session or weekly scope, and SHALL report `unavailable` when no compatible source is present.

#### Scenario: Session data without weekly quota
- **WHEN** the installed Antigravity source exposes session data but no weekly quota
- **THEN** the observation includes the session data and does not invent a weekly window

#### Scenario: No compatible source
- **WHEN** Antigravity is absent or its local interface is incompatible
- **THEN** the adapter is disabled and reports `unavailable` without affecting Codex or OpenRouter

### Requirement: OpenRouter account and key collection
The OpenRouter adapter SHALL collect account credits and authenticated key usage as independent result streams. A successful result from either endpoint SHALL remain available when the other endpoint fails or cannot be called with the available credential. It SHALL derive account remaining credits only when both `total_credits` and `total_usage` are present, and SHALL never add account and key usage together.

#### Scenario: Both OpenRouter endpoints available
- **WHEN** account credit and current-key responses succeed
- **THEN** the observation contains separate `account` and `key` budget records with their reported USD values

#### Scenario: Missing credit field
- **WHEN** either total account credits or total account usage is absent
- **THEN** remaining account credits is `null` rather than a fabricated balance

#### Scenario: Only current-key access succeeds
- **WHEN** the service can read the current key but lacks a working management key for account credits
- **THEN** it retains the current-key result, reports the account-credit error separately, and does not invent an account budget

#### Scenario: One OpenRouter endpoint fails after success
- **WHEN** one OpenRouter endpoint fails after a prior successful result while the other succeeds
- **THEN** the successful result is updated and the failed endpoint retains its last good result with its own freshness state

### Requirement: Redacted capability report
Before enabling GX10 collection, the setup process SHALL produce a redacted report of source versions, endpoint success or error category, available bucket names, and field presence for Codex, Antigravity, OpenRouter, and Tailscale. The report SHALL exclude secret values and complete raw provider responses.

#### Scenario: Host interface validation
- **WHEN** the GX10 capability check runs
- **THEN** the resulting report identifies supported sources and missing fields without exposing credentials or full payloads
