# Docker-Manager Changelog

## v2.0.0

### Added

- `docker-stack login` for browser-based Docker-Manager authentication.
- `docker-stack shell` for isolated manager shells with scoped `DOCKER_CONFIG` and `DOCKER_CONTEXT`.
- `docker-stack setup-auth` for CI and other non-interactive manager auth flows.
- `docker-stack context use <name>` for cleanup-aware switching away from manager contexts.
- GitHub Actions support through the repo composite action in [../action.yml](/Users/sudipbhattarai/Documents/mesudip/docker-stack/action.yml).

### Manager API Integration

- Added manager-native stack listing, version lookup, compose retrieval, validation, deploy, rollback, and node listing.
- Added manager feature gating so fast paths are enabled only when the latest manager advertises the current API surface.
- Removed legacy manager compatibility fallbacks and old backend detection heuristics.

### Behavior

- Manager login keeps auth available across shells for the selected manager context.
- Switching to a non-manager context through `docker-stack context use` clears the global manager auth header.
- Latest Docker-Manager is supported; older manager variants are no longer targeted.

### CI

- Added support for access-token and GitHub OIDC manager auth bootstrapping.
- Added isolated Docker config directory handling for workflow runners.

### Fixed

- Context discovery now fails closed when inspected Docker context payloads are malformed.
- Manager login and switching guidance now matches the actual auth-header lifecycle.
