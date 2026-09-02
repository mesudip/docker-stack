# Changelog

## v2.3.0

### Fixed

- Manager-backed `deploy` and `rollback` no longer fail with a raw `HTTP 409` when another deploy of the same stack is running. Docker-Manager now serializes applies per stack and answers the interactive stream and rollback paths with `409 deployment_in_progress` instead of racing; `docker-stack` queues behind that run, prints who started it, and retries until it finishes or `DOCKER_MANAGER_DEPLOY_WAIT_SECS` runs out (default: the deploy timeout, `0` fails immediately). The GitHub Action image-only deploy does the same.
- Manager deploy failures now list every failed service with its error and incident id. Docker-Manager runs service operations in parallel and reports `deployment_partial_failure` with per-service results; the CLI used to print only the generic summary line and drop the causes. When the manager offers a rollback it prints the deployment id to use on the stack page.
- The manager's `deployment` stream event is printed, so a CLI deploy can be matched to its entry in the Docker-Manager deployment history.

### Added

- While `deploy` or `checkout` waits for another run of the same stack, pressing Enter twice at a terminal forces the deploy: the CLI asks Docker-Manager to abort the running deployment (`POST /api/stacks/deploy/abort`) and deploys as soon as the stack is released. Force is offered once per run, needs stack deploy permission, and every outcome (aborted, already finished, not released, no permission) is reported. Nothing changes for non-interactive runs, which keep waiting.

- `docker-stack node use <node>` no longer fails with "Docker-Manager does not support cluster container CLI". Node selection now checks only the node being selected: an unrelated node that is drained, down, or missing its agent can no longer make a usable node - including the manager's own - look unusable. The failure that is reported is the concrete reason that specific node is unreachable.
- Endpoint discovery no longer swallows its cause. When a command must report a failure, it says whether no Docker-Manager endpoint could be resolved, or the endpoint is not a Docker-Manager, instead of blaming the manager version.
- `docker ps` now reports nodes that were skipped because they cannot host the agent, alongside the existing per-node errors.

### Changed

- Only the current Docker-Manager is supported. `cluster_container_cli_v1` is no longer negotiated; the CLI checks that the target is a Docker-Manager rather than which version it is.

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
