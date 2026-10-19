## Purpose

Give macOS and iOS widgets small, stable, read-only JSON responses from cached usage data without waiting for provider requests.

## ADDED Requirements

### Requirement: Cached current usage
`GET /api/v1/usage` SHALL return an `observations` array containing only sources with at least one successful collection. Each entry SHALL be the source's latest successful observation, including `status`, `collectedAt`, and `staleAfter`. Serving this endpoint SHALL NOT trigger or wait for a provider poll. A source that has never succeeded SHALL have no entry in this array; its failure SHALL remain visible through `/health`.

#### Scenario: Provider outage during widget request
- **WHEN** a widget requests current usage after a source fails but has a prior successful observation
- **THEN** the response includes that source's retained observation and freshness state while other sources' data remains present

#### Scenario: First collection fails
- **WHEN** a widget requests current usage and a source has never collected successfully
- **THEN** the source has no entry in `observations` and its failure appears only in `/health`

#### Scenario: No source has succeeded
- **WHEN** a widget requests current usage before any source has collected successfully
- **THEN** the response contains `observations: []`

### Requirement: Bounded usage history
`GET /api/v1/usage/history?hours=24` SHALL return cached observations in ascending `collectedAt` order. Its `hours` parameter SHALL default to 24 and have a maximum of 168. Its optional `limit` parameter SHALL default to 100 and have a maximum of 500. The response SHALL include `truncated: true` when more matching records exist than can be returned under the count or byte bounds. Invalid query values SHALL receive an `invalid_query` error.

#### Scenario: Oversized history request
- **WHEN** a client requests a lookback beyond the configured maximum
- **THEN** the service returns `invalid_query` without an unbounded response

### Requirement: Health visibility
`GET /health` SHALL report service state and each source's state, including separate OpenRouter account-credit and current-key states, without suppressing healthy sources.

#### Scenario: One adapter fails
- **WHEN** one provider has an error and two are healthy
- **THEN** health reports that source's error category and the states of every configured source

#### Scenario: Partial OpenRouter access
- **WHEN** current-key collection succeeds and account-credit collection cannot run
- **THEN** health reports separate results for `openrouter-key` and `openrouter-account`

### Requirement: Versioned and private JSON contract
All endpoints SHALL be read-only JSON with `schemaVersion: 1` and UTC ISO 8601 timestamps. Successful `/api/v1/usage` and history responses SHALL have `schemaVersion`, `generatedAt`, and `observations`; history SHALL also have `hours` and `truncated`. Each observation SHALL have `provider`, `source`, `accountId`, `observedAt`, `collectedAt`, `staleAfter`, `status`, `quotaWindows`, and `budgets`. `status` SHALL be `fresh` or `stale`. Unknown scalar values SHALL be `null`, and unused arrays SHALL be empty.

`schemaVersion` SHALL be a number, `generatedAt` SHALL be a timestamp string, and `observations` SHALL be an array. `provider` SHALL be `codex`, `antigravity`, or `openrouter`; `source` SHALL match one of the stable source IDs in `/health`. `accountId` and `observedAt` SHALL be strings or `null`; `collectedAt` and `staleAfter` SHALL be timestamp strings. Current observations SHALL be ordered by source ID. History `hours` SHALL be a number and `truncated` a boolean.

Each quota window SHALL have `id`, `label`, `scope`, `group`, `model`, `usedPercent`, `remainingPercent`, `durationMinutes`, `resetsAt`, and `pace`. Each budget SHALL have `id`, `scope` (`account` or `key`), `totalCreditsUsd`, `totalUsageUsd`, `remainingUsd`, `limitUsd`, `usedUsd`, `resetsAt`, and `pace`. USD values SHALL be decimal strings or `null`. A `pace` value SHALL have `classification` (`under`, `at`, `over`, or `unknown`), `basis` (`reported`, `estimated`, or `none`), and `expectedUsedPercent` (number or `null`).

Quota and budget IDs SHALL be opaque strings. Labels, scopes, groups, and models SHALL be strings or `null`; percentages and duration SHALL be numbers or `null`; reset times SHALL be timestamp strings or `null`. `pace` SHALL always be an object, including when its classification is `unknown`.

`/health` SHALL have `schemaVersion`, `generatedAt`, `serviceStatus` (`healthy` or `degraded`), and `sources`. Each source state SHALL have `id`, `provider`, `status` (`healthy`, `retrying`, `operator_action`, or `unavailable`), `lastAttemptAt`, `lastSuccessAt`, and `errorCategory`. Source IDs SHALL include `codex`, `antigravity`, `openrouter-account`, and `openrouter-key`. `errorCategory` SHALL be `null` or one of `authentication`, `authorization`, `rate_limited`, `timeout`, `network`, `invalid_response`, `unsupported`, `not_configured`, or `internal`.

`sources` SHALL be an array containing all four source IDs, including disabled sources. Last-attempt and last-success fields SHALL be timestamp strings or `null`. `serviceStatus` SHALL be `healthy` only when every source is healthy; otherwise it SHALL be `degraded`. A reachable `/health` endpoint SHALL return HTTP 200 for degraded provider state.

Error responses SHALL have `schemaVersion`, `generatedAt`, and an `error` object with `category` and a safe `message`. HTTP error categories SHALL be `invalid_query`, `not_found`, `method_not_allowed`, `response_too_large`, `storage_unavailable`, or `internal`. Responses SHALL exclude credentials, raw provider payloads, email addresses, and stack traces. The service SHALL apply cache and response-size bounds appropriate for widgets.

The service SHALL use HTTP 400 for `invalid_query`, 404 for `not_found`, 405 for `method_not_allowed`, 413 for `response_too_large`, 503 for `storage_unavailable`, and 500 for `internal`. The response body SHALL remain valid JSON within the configured byte cap; if one observation alone exceeds that cap, the service SHALL return `response_too_large`.

#### Scenario: Error response
- **WHEN** a request is invalid or the service cannot satisfy it
- **THEN** the JSON response includes the schema version and a stable error category without sensitive diagnostic content

#### Scenario: Independent OpenRouter results
- **WHEN** only one OpenRouter endpoint has ever succeeded
- **THEN** `/api/v1/usage` contains an observation for that endpoint and none for the other
