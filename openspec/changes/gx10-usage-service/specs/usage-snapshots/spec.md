## Purpose

Keep a safe, normalized history of provider usage and expose meaningful freshness and pace even when some sources fail temporarily.

## ADDED Requirements

### Requirement: Normalized observations
Each observation SHALL contain `provider`, opaque or null `accountId`, `source`, `observedAt`, `collectedAt`, and `status`. Each independently collected OpenRouter endpoint SHALL have its own observation. Quota windows SHALL preserve `id`, `label`, `scope`, usage or remaining percentage, duration, reset time, and pace when available. Unknown values SHALL be `null`; the service SHALL NOT infer zero usage or a full remaining quota from absent fields.

#### Scenario: Missing usage field
- **WHEN** a source reports a quota window without a measured usage fraction
- **THEN** the normalized window retains the known fields and represents the missing fraction as `null`

### Requirement: Safe account transitions
The service SHALL distinguish observations from different provider accounts by an opaque account identifier when one is available, and SHALL NOT combine history or inferred spending across a detected account change.

#### Scenario: Provider account changes
- **WHEN** a provider begins reporting a different account identifier
- **THEN** the new observation is stored separately and calculations exclude the previous account's observations

### Requirement: Last good snapshot and freshness
The service SHALL retain each source's last successful observation after a failed poll, record each source's last attempt, last success, and error category, and mark a retained observation `stale` after two missed configured intervals. A source with no successful observation SHALL have no usage observation; its health state SHALL report `retrying`, `operator_action`, or `unavailable` as appropriate.

#### Scenario: Temporary collection failure
- **WHEN** a provider poll fails before two intervals have been missed
- **THEN** the last good observation remains readable with its original collection time and the failure is visible in source health

#### Scenario: Stale or missing data
- **WHEN** two configured intervals pass without a successful poll
- **THEN** a retained observation is marked `stale`, or no usage observation exists if that source has never succeeded

#### Scenario: First collection fails
- **WHEN** a source fails before producing any successful observation
- **THEN** no usage observation is created for it and its error is visible only through health

### Requirement: Bounded, private observation history
The service SHALL persist successful normalized observations on persistent GX10 storage, retain a bounded history, and SHALL NOT persist credentials, CSRF tokens, complete provider payloads, or personally identifying account fields other than an opaque account ID.

#### Scenario: Successful poll
- **WHEN** a provider returns a valid normalized observation
- **THEN** that observation is stored and becomes that source's latest successful observation

#### Scenario: History retention limit
- **WHEN** stored observations exceed the configured retention bound
- **THEN** older observations are removed without affecting the latest observation for each source

### Requirement: Quota pace classification
For a quota with measured used percentage and reliable start and end, the service SHALL calculate expected used percentage from elapsed fraction of the window, clamped to 0–100, and classify actual usage as `under`, `at`, or `over` using a configurable tolerance that defaults to five percentage points.

#### Scenario: Reliable quota window
- **WHEN** a quota has measured used percentage and reliable start and end timestamps
- **THEN** pace compares measured use with elapsed-window expectation and includes the resulting classification

### Requirement: Estimated or unknown pace
When only reset time and duration are reported, the service SHALL estimate the start from those fields and label pace as estimated. It SHALL return `unknown` when duration, reset, or measured usage is missing, when reset timing is implausible, or when the source reports only model availability.

#### Scenario: Inferred start time
- **WHEN** a window reports a plausible reset time and duration but no explicit start
- **THEN** its pace uses the inferred start and is marked `estimated`

#### Scenario: Reset time that tracks the clock
- **WHEN** consecutive snapshots of the same window show its reported reset time advancing with the clock — by far less than a whole window, rather than staying fixed or jumping by a full window
- **THEN** pace is `unknown`, because such a report names no fixed boundary and the inferred start would always be the current time, leaving the elapsed fraction permanently zero

#### Scenario: Availability without usage
- **WHEN** a source reports only a 100% model availability indicator
- **THEN** pace is `unknown` and the indicator is not treated as 100% quota remaining

### Requirement: OpenRouter budget pace
The service SHALL calculate OpenRouter budget pace only when a key limit and exact reset-period boundaries are known. Otherwise it SHALL present known balance and locally observed recent spend, and SHALL label any optional projection as a projection.

#### Scenario: Monthly label without exact boundary
- **WHEN** a key reports `limit_reset: "monthly"` without an exact reset timestamp
- **THEN** the service does not infer an exact period boundary or exact budget pace
