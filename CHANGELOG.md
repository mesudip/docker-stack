# Changelog

## v2.1.0

### Added

- Secret definitions can now use Docker Compose-style `environment` entries so secret content is read from environment variables at deploy time.
- `x-generated` is now accepted as an alias for `x-generate` on generated secrets.
- Versioned stack state now stores source metadata in `x-files`, including the original compose file, referenced non-secret environment values, and config source files used by `file` / `x-template-file`.
- GitHub Actions deployments now use `actions/setup-python@v6` before installing `docker-stack`, avoiding PEP 668 failures from externally managed system Python installs while keeping Node.js post-action cleanup.

### Changed

- Manager-backed deploys now preserve source metadata for rollback and inspection while stripping `x-files` before the actual Docker stack deploy payload.
- `docker-stack deploy --with-registry-auth` now forwards matching registry credentials to Docker-Manager deploys, including credentials resolved through Docker credential helpers.
- Docker-Manager stack queries and rollbacks now use direct manager API paths when the manager advertises the enterprise feature set.
- Manager shell startup now clears inherited Docker endpoint override environment variables so the selected Docker context is authoritative.
- GitHub Actions release publishing now runs only for full semantic release tags such as `v2.1.0`, not moving tags such as `v2`.

### Fixed

- `docker-stack login` now reports already-active manager logins cleanly and can refresh the current isolated shell config.
- Browser login can accept a pasted callback URL when the browser cannot reach the local callback listener.
- Existing Docker configs or secrets without docker-stack labels are versioned safely instead of being overwritten or reused incorrectly.
- `x-template` processing for configs and secrets now uses the declared template content.

## v2.0.0

### Added

- Improved `.env` loading with ordered reference resolution, quote handling, long dependency-chain support, and clearer missing-variable / cycle errors.
- Better stack and node inspection output for daemon workflows.
- `--namespace` support for stack commands where applicable.
- Docker-Manager support and a GitHub Action for it.

### Fixed
- Node listing is easier to scan.
- Stack and version output are formatted more consistently.
- `cat` now resolves the latest version cleanly without printing the versions table first.
