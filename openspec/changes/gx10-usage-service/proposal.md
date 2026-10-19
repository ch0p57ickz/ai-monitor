## Why

macOS and iOS widgets need a consistent, timely view of Codex, Antigravity, and OpenRouter usage. The GX10 can collect those sources centrally, retain the last successful readings through provider outages, and serve bounded responses without making widgets depend on live provider calls.

## What Changes

- Add independent, configurable collectors for Codex, Antigravity, and OpenRouter, with separate OpenRouter account and key results and host capability checks before enabling each source.
- Normalize provider quota windows and credit budgets, preserve unknown fields, and retain successful observations in SQLite.
- Calculate quota pace only from sufficient usage and window data; label estimates and unknown pace explicitly.
- Expose successful cached usage, bounded history, and per-source health through a defined, versioned, read-only JSON API; omit sources with no successful reading from usage responses.
- Run the service on GX10 with local binding, persistent storage, and restricted HTTPS access through Tailscale Serve.

## Capabilities

### New Capabilities

- `provider-usage-collection`: Discover, poll, and normalize the three provider sources independently.
- `usage-snapshots`: Persist normalized observations, retain last good values, and report freshness and pace.
- `widget-usage-api`: Serve cached current usage, bounded history, and health as safe JSON responses.
- `gx10-service-access`: Run and expose the service on GX10 with restricted access and protected credentials.

### Modified Capabilities

None. This repository has no existing OpenSpec capability specs.

## Impact

- Adds a GX10 service under the `com.sushidev` namespace, a SQLite data store, three provider integrations, and a local HTTP API.
- Requires validation of the GX10 runtime, local Codex and Antigravity interfaces, OpenRouter key permissions, and Tailscale configuration before deployment.
- Adds operational guidance for service lifecycle, backup, and credential rotation after host details are known.

Source: Rumi KB `inbox/gx10-usage-service-spec.md` (draft dated 2026-10-19).
