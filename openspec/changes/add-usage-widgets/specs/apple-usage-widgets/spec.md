## Purpose

Show the service's cached provider usage on iPhone and Mac in glanceable widgets while clearly communicating freshness, missing values, and connectivity failures.

## ADDED Requirements

### Requirement: Private service setup
The app SHALL let the user save a valid HTTPS service base URL and SHALL use that URL for widget requests even when the service is temporarily unreachable. It SHALL check `/api/v1/usage` to confirm connectivity and version 1 compatibility when reachable. It SHALL explain that the device needs access to the private service and SHALL reject malformed or non-HTTPS URLs. The client SHALL NOT require or store provider credentials.

#### Scenario: Configure reachable service
- **WHEN** the user saves a valid HTTPS service URL and the device can reach it
- **THEN** the app confirms connectivity and the widgets can use that service

#### Scenario: Invalid service URL
- **WHEN** the user enters a malformed or non-HTTPS URL
- **THEN** the app rejects it and retains the previous valid setting

### Requirement: Current usage display
The widgets SHALL read version 1 current usage from the configured service. They SHALL display only `codex`, `openrouter-account`, and `openrouter-key`; they SHALL omit `antigravity`. A small widget SHALL show one user-selected displayed source. A medium widget SHALL show a row for each of the three displayed sources, including a waiting row for any missing Codex or OpenRouter observation. Each available row SHALL show a quota or budget value, its unit or scope, and the collection time. An absent or null value SHALL display as unknown rather than zero. Multiple quota windows from one source SHALL remain distinguishable by their reported labels or scopes.

#### Scenario: Quota source has multiple windows
- **WHEN** a source reports multiple quota windows
- **THEN** the widget identifies the displayed window and does not merge their percentages

#### Scenario: OpenRouter budget
- **WHEN** an OpenRouter observation contains a budget with `remainingUsd`
- **THEN** the widget displays the remaining USD amount with its account or key scope

#### Scenario: Value is unknown
- **WHEN** the selected metric is null or a source has no matching metric
- **THEN** the widget displays an unknown value without implying a full or empty quota

#### Scenario: Source has not collected successfully
- **WHEN** the API has no observation for `codex`, `openrouter-account`, or `openrouter-key`
- **THEN** the medium widget shows a waiting row for that source

#### Scenario: Antigravity observation is present
- **WHEN** the API includes an `antigravity` observation
- **THEN** neither widget offers or displays that source

### Requirement: Freshness and failure visibility
The widgets SHALL identify stale observations using the API's `status` and `collectedAt`. Only an HTTP 200 response with a valid version 1 usage payload SHALL replace the retained display. An HTTP error, malformed payload, or request failure SHALL preserve a previously displayed observation with a visible last-updated time and connection warning. A valid empty `observations` array SHALL replace the previous display and show waiting rows. Before any successful observation, the widget SHALL show a setup or waiting state rather than fabricated usage. An unsupported schema version SHALL show a compatibility error and SHALL NOT replace the retained display. Changing the service URL SHALL NOT clear or hide the retained display; until the new service responds successfully, the widget SHALL identify it as previously collected data and show a connection or compatibility warning.

#### Scenario: Provider collection stops
- **WHEN** the API returns a retained observation with `status: stale`
- **THEN** the widget shows the value with a stale indicator and its collection age

#### Scenario: Service becomes unreachable
- **WHEN** a refresh fails after a successful display
- **THEN** the widget keeps the last successful value and marks the display as disconnected

#### Scenario: No successful source
- **WHEN** the API returns an empty `observations` array and no prior display exists
- **THEN** the widget shows a waiting state with a path to the app for details

#### Scenario: Empty current response after prior data
- **WHEN** a valid version 1 response has an empty `observations` array after a prior successful display
- **THEN** the widget replaces the prior display with waiting rows

#### Scenario: Service URL changes while unreachable
- **WHEN** the user saves a new valid service URL and the next request fails
- **THEN** the widget retains the previous display, labels it as previously collected data, and shows a connection warning

#### Scenario: Invalid usage response
- **WHEN** the service returns an HTTP error or a malformed usage payload
- **THEN** the widget does not replace its retained display and shows a connection or response warning

### Requirement: Bounded refresh and privacy
The widget SHALL fetch only the cached read-only API during its scheduled refresh and SHALL avoid provider polling or embedding secrets. Each request SHALL time out within 10 seconds. The widget SHALL retain at most one compact display payload for 2560 seconds after its last successful fetch, then show a waiting state. Widget content SHALL respect the platform's privacy redaction state when the device is locked; the app SHALL NOT introduce a separate sign-in state.

#### Scenario: Widget refresh
- **WHEN** the system requests a new widget timeline
- **THEN** the client attempts one bounded current-usage request and schedules a later refresh without assuming an exact execution time

#### Scenario: Widget is privacy redacted
- **WHEN** the device is locked and the platform requests a redacted widget display
- **THEN** usage amounts and account identifiers are hidden
