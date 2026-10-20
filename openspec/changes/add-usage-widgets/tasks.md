## 1. Apple project and shared client

- [ ] 1.1 Add iOS 27 and macOS 27 app and widget extension targets under `com.sushidev`; verify the project opens and all targets build in Xcode.
- [ ] 1.2 Add shared decodable API models and a bounded HTTPS client for version 1 `/api/v1/usage` with a 10-second timeout; verify fixture tests cover the three displayed sources, ignored Antigravity, null fields, HTTP errors, malformed payloads, and an unsupported schema version.
- [ ] 1.3 Add App Group storage for a validated base URL and one compact last good display retained for 2560 seconds after its last successful fetch; verify app and extension targets can read the same setting and cache, and a URL change does not clear or hide the retained display.

## 2. Setup and presentation

- [ ] 2.1 Build the app's service setup and connectivity screen; verify valid HTTPS URLs can be saved while unreachable, invalid URLs retain the prior setting, `/api/v1/usage` confirms a compatible service when reachable, and connection or compatibility failures are explained.
- [ ] 2.2 Add shared metric selection and formatting for quota windows and account/key budgets; verify tests keep windows distinct, format USD decimal strings, and render null values as unknown.
- [ ] 2.3 Build a configurable small widget and a three-row medium widget; verify previews cover Codex, both OpenRouter scopes, ignored Antigravity, and waiting rows for absent displayed sources.
- [ ] 2.4 Add collection age, stale, disconnected, waiting, compatibility, and locked-device privacy-redacted views with an app link; verify previews or UI tests exercise each state without adding an app sign-in flow.

## 3. Refresh and device validation

- [ ] 3.1 Implement the timeline request and retry policy with one bounded usage fetch per refresh; verify tests cover success, timeout, offline cache reuse, cache expiry after 2560 seconds, valid empty responses replacing prior data, URL changes, and later refresh scheduling.
- [ ] 3.2 Document service URL setup and Tailscale access for iOS and macOS; verify the instructions match the deployed API routes and require no provider credentials.
- [ ] 3.3 Build and run both platform widgets on a Tailscale-connected Mac and iPhone, and verify service data, App Group sharing, stale display, offline fallback, and privacy redaction.
