## Purpose

Run the usage API on GX10 with durable local operation and restricted HTTPS access for the intended Mac and iPhone widgets.

## ADDED Requirements

### Requirement: Local service binding
The HTTP service SHALL bind to `127.0.0.1` and SHALL expose only the documented read-only endpoints.

#### Scenario: Host network request
- **WHEN** a device requests the GX10 host port directly over the network
- **THEN** the service is not reachable through that direct host interface

### Requirement: Restricted Tailscale access
The deployment SHALL publish the local port through Tailscale Serve over HTTPS and SHALL restrict access through tailnet identity and device rules to the intended Mac and iPhone. It SHALL NOT enable Funnel.

#### Scenario: Allowed and disallowed devices
- **WHEN** access is tested from an allowed device and a disallowed device
- **THEN** the allowed device can reach the HTTPS API and the disallowed device cannot

### Requirement: Protected service operation
The service SHALL run under a dedicated unprivileged account with automatic restart and a persistent data directory. Provider credentials SHALL stay in that account's supported credential mechanism and SHALL NOT appear in HTTP responses or logs.

#### Scenario: Service restart
- **WHEN** the service process restarts
- **THEN** it resumes using persisted snapshots and credential access without exposing secret values

### Requirement: Operational documentation
After GX10 environment validation, deployment guidance SHALL document service start, upgrade, backup, and credential rotation procedures.

#### Scenario: Deployment handoff
- **WHEN** the service is prepared for GX10 operation
- **THEN** an operator has host-specific commands for each documented lifecycle procedure
