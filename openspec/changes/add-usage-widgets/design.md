## Context

See [proposal.md](proposal.md) for motivation and [spec.md](specs/apple-usage-widgets/spec.md) for behavior. The Python service already serves cached, versioned JSON at `/api/v1/usage`, with up to four independent source IDs. It binds locally and is published as a private HTTPS Tailscale Service. This repository has no Apple app or Xcode project yet.

## Goals / Non-Goals

**Goals:**

- Keep the widget request path independent of provider polling and credentials.
- Share decoding, display selection, and cache behavior across iOS and macOS.
- Make the source age and network state visible without inventing values.

**Non-Goals:**

- Background polling outside WidgetKit's timeline requests.
- Editing provider settings or exposing the service publicly.
- A history chart in the first release.

## Decisions

### 1. Use one multiplatform SwiftUI app with a WidgetKit extension

Commit an Xcode project with iOS 27 and macOS 27 app targets and widget extension targets sharing a small Swift package or source group. Use the `com.sushidev` namespace for bundle identifiers. Use an App Intent parameter for the small widget's source selection among `codex`, `openrouter-account`, and `openrouter-key`; the medium widget has a row for each of those three source IDs, including waiting rows when an observation is absent. This fits the native widget gallery and avoids maintaining separate rendering logic. A web view or menu bar only client would not give the requested home screen and desktop widgets.

### 2. Put configuration and last good display in an App Group

The app validates an HTTPS base URL, writes it to App Group storage even when unreachable, and checks `/api/v1/usage` to report connectivity and version 1 compatibility when reachable. The extension reads the same setting and stores a compact decoded last good display with a fetch timestamp. Keep only fields needed for rendering, cap the cache to one current response, and retain it for 2560 seconds after the last successful fetch. Changing the URL leaves this display available until a valid response replaces it or retention expires; the widget labels it as previously collected data while the new service is unreachable. Only a valid HTTP 200 version 1 usage response replaces the display, including a valid empty observation array. On HTTP errors, malformed responses, or request failures, render the retained display with a warning. On an unsupported schema version, show a compatibility state and do not treat the payload as current. This avoids coupling the widget to the app's process lifetime; per-target defaults would not be shared reliably.

### 3. Decode the API strictly at the boundary and format conservatively

Model `schemaVersion`, `observations`, source IDs, freshness, quota windows, and budgets with optional fields matching the server contract. Reject an unsupported schema version. Ignore `antigravity` for widget presentation. Preserve the server's source IDs and window labels; for a small Codex widget, show up to two windows ordered by shortest reported duration then ID, and show the account or key budget for an OpenRouter source. The medium widget uses three compact source rows with the first Codex window under that same ordering or the matching OpenRouter budget as its primary metric, plus age; it opens the app for full details. Show `unknown` for absent values; parse USD decimal strings for formatting without using floating point for arithmetic. Do not calculate remaining values or pace in the widget when the server has not supplied them. Alternative client side aggregation would risk mixing unrelated quota windows or scopes.

### 4. Fetch once per timeline request, then request a later refresh

Use `URLSession` with a 10-second timeout to fetch `/api/v1/usage` and create one timeline entry. Request another timeline around the service's normal polling cadence, with a conservative retry delay after failure; treat that date as an earliest request, not a guarantee. Use the stored entry for gallery snapshots and offline rendering. WidgetKit controls actual execution, so a timer or promised minute-by-minute freshness would be misleading.

### 5. Keep access and presentation private

The client uses the private HTTPS service URL and relies on the device's Tailscale connectivity and service policy. It stores no provider credentials and uses no public relay. When the device is locked and the platform requests privacy redaction, blur or hide usage amounts and opaque account identifiers in widget views. The app has no separate sign-in state. Keep log messages to status categories and never log response bodies or configured URLs. This preserves the service's private access model.

## Risks / Trade-offs

- **Widget refresh arrives late** → Show `collectedAt` and the server's stale state; do not claim exact refresh timing.
- **Tailscale is disconnected on a device** → Render the bounded last good cache with an explicit connection warning and an app link for diagnostics.
- **Apple build tools are unavailable in this workspace** → Keep project sources reviewable here; validate target builds and widget behavior on a Mac during implementation.
- **Three sources exceed a compact layout** → Keep medium rows terse and move full window and budget detail into the app.
- **App Group or entitlement mismatch** → Add a device test that saves a URL in the app and confirms the extension reads it.

## Migration Plan

1. Add the Apple targets and shared model code without changing the Python service or its API.
2. Build and run on macOS and iOS, then verify the private service URL on a Tailscale-connected device.
3. Release the app and widget together. Rollback removes the widget/app; no server migration is needed.
