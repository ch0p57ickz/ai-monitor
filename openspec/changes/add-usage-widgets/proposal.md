## Why

The service exposes cached usage data for widgets, but there is no client that puts it on an iPhone or Mac at a glance. A small Apple widget can show remaining quota and credit balance while making stale or unavailable data clear.

## What Changes

- Add an iOS and macOS app with a WidgetKit extension that reads the existing versioned usage API over the tailnet.
- Show Codex and OpenRouter quota and credit summaries in small and medium widgets, with collection age, freshness, and honest unknown or waiting states. Antigravity is outside the widget display scope.
- Preserve the last successful display for up to 2560 seconds during temporary request failures, including after a service URL change, and provide a useful waiting state when the service returns no observations.
- Provide setup guidance for the service URL and tailnet connectivity.

## Capabilities

### New Capabilities

- `apple-usage-widgets`: Widget presentation, refresh, connectivity, and error behavior on iOS and macOS.

### Modified Capabilities

None. The existing API contract remains unchanged.

## Impact

- New Swift app and WidgetKit targets, shared API models and client, and platform build configuration.
- The widget consumes `GET /api/v1/usage`; an app detail view may consume `GET /health` for diagnostics. No provider credentials enter the client.
- Targets iOS 27 and macOS 27. Requires an Apple development environment for device builds and a device with access to the private Tailscale service.
