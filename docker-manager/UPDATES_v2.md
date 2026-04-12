# Docker-Manager Updates for v2

## Summary

`docker-stack` v2 adds first-class Docker-Manager support while keeping plain Docker daemon workflows available. The release introduces interactive login, isolated manager shells, CI-friendly auth setup, manager-aware stack operations, cleanup-aware context switching, and a reusable GitHub Action.

This release also tightens the support contract: `docker-stack` now targets the latest Docker-Manager API surface rather than carrying compatibility shims for older manager versions.

## What’s New

### Docker-Manager login from the CLI

```bash
docker-stack login
docker-stack login --context office 172.31.0.6:2378
```

This flow:

- opens the manager-brokered browser login
- creates or updates a Docker context for the manager
- switches to that context
- stores the manager bearer header in Docker config so the authenticated context keeps working across shells

### Isolated manager shells

```bash
docker-stack shell --context office 172.31.0.6:2378
docker-stack shell office
```

This is useful when you want manager access without modifying your global shell state outside that subshell.

### CI and automation setup

`docker-stack setup-auth` supports:

- pre-issued manager access tokens
- GitHub OIDC tokens
- isolated Docker config directories

The repo also now ships a reusable GitHub Action:

```yaml
- uses: mesudip/docker-stack@v2
  with:
    manager: 172.31.0.6
    verify-ssl: false
```

### Manager-aware stack fast paths

When the active endpoint is the latest Docker-Manager and advertises the current feature set, `docker-stack` can use manager-native APIs for:

- stack listing
- stack version lookup
- compose retrieval
- deploy validation
- deploy
- rollback
- node listing

### Cleanup-aware context switching

```bash
docker-stack context use default
```

Use this instead of raw `docker context use` when leaving a manager-authenticated context. `docker-stack` clears the global manager auth header when switching to a non-manager context and preserves it when switching between manager contexts created by the tool.

### Namespace support

Manager-aware stack commands now accept `--namespace` on:

- `deploy`
- `cat`
- `checkout`
- `version` / `versions`

## Breaking Changes

### Latest Docker-Manager only

Supported combinations are now:

- latest Docker-Manager
- plain Docker daemon

Older manager builds that do not expose the current brokered login flow or current feature-advertised API surface are no longer supported.

### Context switching expectations changed

After `docker-stack login`, manager auth is intentionally available across shells for the configured manager context. To switch back to a non-manager context cleanly, use:

```bash
docker-stack context use default
```

Do not rely on raw `docker context use default` alone if you want the manager auth header removed from Docker config.

## Upgrade Guide

### If you use Docker-Manager interactively

1. Upgrade Docker-Manager to the latest supported release.
2. Run `docker-stack login`.
3. Use `docker-stack context use <context>` when leaving the manager context.

### If you use Docker-Manager in CI

1. Adopt the repo’s composite action or `docker-stack setup-auth`.
2. Use GitHub OIDC or a manager access token.
3. Set `verify-ssl: true` when you require verified HTTPS only.

## Release Blurb

`docker-stack v2` adds first-class Docker-Manager support with CLI login, isolated shells, CI auth setup, manager-native stack queries and deploy flows, cleanup-aware context switching, and a reusable GitHub Action. Docker-Manager support now targets the latest manager API surface only.
