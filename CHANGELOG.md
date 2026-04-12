# Changelog

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
