# Docker-Manager Changelog

## v2.1.0

### Added

- GitHub Actions support is now implemented as a Node.js action with setup and post-cleanup phases.
- The action now exports `DOCKER_CONFIG`, `DOCKER_CONTEXT`, and `DOCKER_MANAGER_URL` for later workflow steps and removes its generated Docker config directory after the job.
- Manager deploys now receive matching registry auth when `docker-stack deploy --with-registry-auth` is used, including credentials from Docker credential helpers.
- Manager-backed versioned stack config now stores source metadata in `x-files` for compose/config recovery and audit workflows.

### Changed

- The GitHub Action defaults to the `dm-proxy` context when no context is provided.
- Docker-Manager enterprise feature detection now enables direct stack query, node listing, compose retrieval, and rollback API paths.
- Manager deploy validation and deploy requests now use the enriched stored compose for manager state while keeping deployment content clean of source metadata.
- Isolated manager shells now clear inherited Docker endpoint override variables before opening the shell.

### Fixed

- Browser login can continue with a pasted callback URL when automatic localhost callback delivery is unavailable.
- `docker-stack login` now handles already-active manager tokens without forcing a fresh browser login.
- Switching or refreshing an already-active isolated manager shell now reuses the current Docker config directory.

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
