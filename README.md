# Docker Stack CLI Utility

A command-line tool for advanced Docker Swarm stack deployments on plain Docker daemons. `docker-stack` extends vanilla `docker stack deploy` with generated secrets, templated configs, versioned stack state, safer rollbacks, and better day-to-day stack workflows.

## Installation

Install or upgrade `docker-stack` with:

```bash
pip install docker-stack --upgrade --break-system-packages
```

## Quick Start

### Plain Docker Daemon

If you already have a Docker Swarm daemon or Docker context, you can use the advanced stack features directly against it.

Typical daemon-only workflow:

```bash
docker-stack deploy my-stack docker-compose.yml
docker-stack ls
docker-stack ls -n team-a
docker-stack ls -A
docker-stack versions my-stack
docker-stack cat my-stack
docker-stack checkout my-stack v2
docker-stack node ls
```

What this gives you on a raw Docker daemon:

-   richer secret and config handling in Compose
-   generated secrets without external scripts
-   template expansion from env vars and files
-   versioned stack config history
-   stack version inspection and checkout
-   raw daemon compatibility without extra infrastructure

### GitHub Actions

#### 1. Normal Docker daemon

Use this when the runner already has Docker access through the default Docker context or `DOCKER_HOST`.

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v6
    with:
      python-version: '3.x'
  - run: python3 -m pip install --upgrade docker-stack
  - run: docker-stack deploy my-stack docker-compose.yml
```

Use this option when CI can connect directly to the target Docker daemon.

#### 2. Docker-Manager

Use the bundled action when deploying through
[Docker-Manager](https://github.com/mesudip/docker-enterprise). The manager
repository contains the server source, installation instructions, and deployment
documentation.

For a full compose deployment directly from CI, use the action to configure
Docker-Manager authentication and then run the normal `docker-stack deploy`
command:

```yaml
permissions:
  contents: read
  id-token: write

steps:
  - uses: actions/checkout@v4
  - uses: mesudip/docker-stack@v2
    with:
      manager: https://manager.example.com:2378
  - run: docker-stack deploy --namespace team-a --with-registry-auth my-stack docker-compose.yml
```

You can also deploy the full compose file through action inputs:

```yaml
permissions:
  contents: read
  id-token: write

steps:
  - uses: actions/checkout@v4
  - uses: mesudip/docker-stack@v2
    with:
      manager: https://manager.example.com:2378
      stack: my-stack
      compose-file: docker-compose.yml
      namespace: team-a
      with-registry-auth: "true"
```

To release new service images without submitting the compose file again:

```yaml
permissions:
  contents: read
  id-token: write

steps:
  - uses: actions/checkout@v4
  - uses: mesudip/docker-stack@v2
    with:
      manager: https://manager.example.com:2378
      stack: my-stack
      namespace: team-a
      with-registry-auth: "true"
      images: |
        api=ghcr.io/acme/api:${{ github.sha }}
        worker=ghcr.io/acme/worker:${{ github.sha }}
```

Use a full compose deployment when the workflow owns the complete stack
definition. Use an image-only deployment when the stack is already managed and
the workflow only needs to release new images. Both forms support namespaces;
the namespace defaults to `default` when omitted.

### Authenticated Docker-Manager shell

Open an isolated Bash or Zsh session for a manager context:

```bash
docker-stack shell office
```

If `office` does not exist yet, the CLI asks for its Docker-Manager URL, creates
the context, authenticates, and opens the shell. You can also provide everything
non-interactively with `docker-stack shell --context office <manager-url>`.

Stack commands use the `default` namespace unless `-n/--namespace` is supplied.
Listings print the selected namespace; `docker-stack ls -A` (or
`--all-namespaces`) lists every visible namespace.

The prompt displays `(docker:office@cluster)`, keeps the selected manager context active,
and refreshes authentication when needed. The session supports `docker` and
`docker compose`; legacy `docker-compose` is not supported. If authentication
expires, run `docker-stack login` again.

Container listing is cluster-aware and includes the owning Swarm node. Select a
node when working with daemon-local resources such as volumes and images:

```bash
docker ps
docker-stack node current
docker-stack node use worker-02
docker volume ls
docker image ls
docker-stack node use cluster
```

Node selection is stored only in the managed shell's isolated Docker
configuration and appears in the prompt as `(docker:office@worker-02)`.
Selected-node requests remain subject to the manager's Docker permissions;
image management is root-only because the manager does not define delegated
image permissions.

Cluster-aware output is enabled after the manager has observed compatible
agents on every discovered node. The manager remembers that capability until it
restarts, so a temporary agent outage does not make the CLI revert to a legacy
feature decision. A partial cluster listing prints the available containers,
reports each failed node on stderr with its incident id, and exits non-zero.

`docker ps --quiet` and `docker ps --format ...` use Docker's native formatter
and therefore do not add the `NODE` column. Explicit Docker overrides such as
`--context`, `--host`/`-H`, and `--config` bypass managed cluster formatting and
are sent unchanged to the Docker CLI.

## What It Adds

Beyond `docker stack deploy`, against a plain Docker daemon:

- **Generated secrets** — no external scripts; see [Object Versioning and Reuse](#object-versioning-and-reuse) for the once-only semantics.
- **Inline and templated configs/secrets** — content in the compose file, or expanded from environment variables and files.
- **Automatic versioning** — configs and secrets are content-hashed and versioned, so edits do not require hand-written `_v2` names.
- **Version inspection and checkout** — `versions`, `cat`, and `checkout` restore a complete recorded stack version, including its configs.
- **Cluster-aware inspection** — stack and node output that reports the owning Swarm node.

### Concurrent deploys (Docker-Manager)

Docker-Manager runs one apply per stack at a time. When `docker-stack deploy`
or `docker-stack checkout` finds another run in progress (a UI deploy, a CI
image bump, another operator), it waits and prints who started it:

```
[manager] waiting: a deploy started by alice 42s ago is still running (waiting up to 300s, set DOCKER_MANAGER_DEPLOY_WAIT_SECS to change)
[manager] press Enter twice to force your deploy (aborts that run; changes it already made to the daemon stay), Ctrl+C to quit
```

- It retries every 5 seconds until the stack is free or `DOCKER_MANAGER_DEPLOY_WAIT_SECS` runs out (default: the deploy timeout; `0` fails immediately). CI runs wait the same way, without the prompt.
- At a terminal, pressing Enter twice within 3 seconds asks the manager to abort the running deployment and deploys as soon as the stack is released. This needs stack deploy permission and is offered once per run. The aborted run stops orchestrating, but daemon requests it already made still complete; your deploy then applies over that state.
- Ctrl+C exits without touching the other run.

## Compose Extensions

`docker-stack` reads extra keys under top-level `configs:` and `secrets:` and
resolves them into real Docker objects before calling `docker stack deploy`.
Standard Compose keys (`file:`, `external:`, `name:`) continue to work.

| Key | Configs | Secrets | Content comes from |
| --- | --- | --- | --- |
| `file:` | yes | yes | the file, verbatim (standard Compose) |
| `x-content` | yes | yes | a literal string in the compose file |
| `x-template` | yes | yes | a literal string, with `${VAR}` expanded |
| `x-template-file` | yes | yes | a file, with `${VAR}` expanded |
| `environment` | no | yes | the named environment variable |
| `x-generate` | no | yes | a value generated by `docker-stack` |

Exactly one content key per object.

### `x-content` — inline content

```yaml
secrets:
  my_inline_secret:
    x-content: "This is my secret content defined inline."

configs:
  my_inline_config:
    x-content: |
      key=value
      another_key=another_value
```

### `x-template` and `x-template-file` — environment substitution

`${VAR}` references are expanded from the deploying shell's environment.

```yaml
secrets:
  my_templated_secret:
    x-template: "${API_KEY_NAME}:${MY_API_KEY}"

configs:
  my_config_from_template_file:
    x-template-file: "./templates/my_config.tpl"
```

### `environment` — content from a variable

```yaml
secrets:
  api_token:
    environment: API_TOKEN
configs:
  app_conf:
    environment: APP_CONF_VALUE
```

If the variable is unset or empty, the deploy fails before any Docker object is
created.

Configs are supported on Docker-Manager deploys, where the value travels in the
stack `.env` and the manager resolves it. A raw daemon deploy supports secrets
only. The two are handled differently on purpose: a secret's value is inlined
locally so it is never written to the `.env` that is stored with the stack.

### `x-generate` — generated secrets

Secrets only. Configs must use `x-content`, `x-template`, or `x-template-file`.

```yaml
secrets:
  # default options, random length 12-20
  simple:
    x-generate: true

  # fixed length, default character classes
  fixed_length:
    x-generate: 30

  # explicit character classes
  api_token:
    x-generate:
      length: 40
      numbers: true    # digits 0-9              (default true)
      special: true    # punctuation             (default true)
      uppercase: true  # A-Z                     (default true)
```

Lowercase letters are always included and the value always starts with a
letter.

`special: true` leaves out `$`, `'`, `"` and `\`, so generated values are safe
to paste into a shell or a YAML file. Use `special: false` when the application
rejects punctuation, or for values that go in URLs or HTTP headers:

```yaml
secrets:
  bearer_token:
    x-generate:
      length: 64
      numbers: true
      special: false
      uppercase: true
```

## Versioning and Reuse

Docker configs and secrets cannot be changed in place. `docker-stack` handles
that for you: when a config's content changes, it creates a new version and
points your services at it. You never have to add `_v2` to names yourself.

**Generated secrets are created once.** Redeploying does not change them, so
anything holding a generated value keeps working. The value survives
redeploys, restarts and unrelated changes to the stack.

### Stored source metadata

Versioned stack configs include a top-level `x-files` list holding
base64-encoded source material for recovery and auditing: the original compose
file as `compose.yml`, a generated `.env` containing referenced non-secret
environment values, and any config files referenced by `configs.*.file` or
`configs.*.x-template-file`.

Secret source files, and the variables named by `secrets.*.environment`, are
deliberately **not** stored in `x-files`.

### Rotating a generated secret

There is no rotate flag. Remove the secret's newest version, then deploy again
to get a fresh value:

```bash
docker secret rm <name>_v<N>
docker-stack deploy --show-generated my-stack docker-compose.yml
```

`--show-generated` prints the new value. Update anything still holding the old
one.

### Secrets you created yourself

If a secret already exists because you ran `docker secret create`, pointing
`x-generate` at it does **not** take it over. The deploy generates a new value
instead, and anything holding the old one stops working.

Either leave it as `external: true`, or move to `x-generate` deliberately:
deploy once with `--show-generated`, then update your clients. After that it
behaves like any other generated secret.

## Known Limitations

Docker limits config content to 500 KB. Stack history includes encoded source
files, so stacks with large compose or config files can exceed that limit.

## Development

Install runtime and test dependencies with either:

```bash
python3 -m pip install -r requirements-dev.txt
```

or:

```bash
python3 -m pip install -e '.[dev]'
```
