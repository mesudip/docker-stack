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

The prompt displays `(docker:office)`, keeps the selected manager context active,
and refreshes authentication when needed. The session supports `docker` and
`docker compose`; legacy `docker-compose` is not supported. If authentication
expires, run `docker-stack login` again.

## Core Capabilities

-   **Advanced Deployments on Plain Docker Daemons:**
    `docker-stack` works directly against a raw Docker daemon and adds capabilities that standard `docker stack deploy` does not provide out of the box:
    - generated secrets
    - inline configs and secrets
    - template rendering from environment variables and files
    - versioned config and secret history
    - version lookup, checkout, and rollback-oriented workflows
    - more ergonomic stack and node inspection output

-   **Docker Stack Versioning and Config Backup for Rollback:**
    The utility automatically versions your Docker configs and secrets, allowing for easy tracking of changes and seamless rollbacks to previous states. This provides a safety net for your deployments, ensuring you can always revert to a stable configuration.

## Why Use It?

Vanilla Docker Stack deployments can sometimes lack the flexibility needed for dynamic environments or robust secret management. This utility bridges those gaps by:

-   **Automating Secret Management:** No more manual secret generation or complex external scripts.
-   **Simplifying Configuration:** Define configs and secrets directly in your compose files or use templates.
-   **Enhancing Security:** Generate strong, random secrets on the fly.
-   **Enabling Rollbacks:** Versioning ensures you can always revert to a known good state.
-   **Improving Raw Daemon Workflows:** Works directly with a plain Docker Swarm daemon.

## Advanced Compose Features

-   **Docker Config and Secret Management with Extended Options:**
    This utility significantly extends Docker's native config and secret management by introducing `x-` prefixed directives in your `docker-compose.yml` files. These directives allow for dynamic content generation, templating, and file inclusion, making your deployments more flexible and secure.

    ### `x-content`: Inline Content for Configs and Secrets
    Allows you to define the content of a Docker config or secret directly within your `docker-compose.yml`.

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

    ### `x-template`: Environment Variable Templating
    Enables the use of environment variables within your config or secret content, which are substituted at deployment time.

    ```yaml
    secrets:
      my_templated_secret:
        x-template: "I can create composite secret with template. ${API_KEY_NAME}:${MY_API_KEY}"
    ```

    ### `x-template-file`: External Template Files
    Reference an external file whose content will be treated as a template and processed with environment variables.

    ```yaml
    configs:
      my_config_from_template_file:
        x-template-file: "./templates/my_config.tpl"
    ```
    *(Content of `./templates/my_config.tpl` might be: `DB_HOST=${DATABASE_HOST}`)*

    ### `environment`: Secret Content from Environment Variables
    Secrets can read their content from an environment variable at deploy time.

    ```yaml
    secrets:
      api_token:
        environment: API_TOKEN
    ```

    If the variable is unset or empty, deployment fails before Docker objects are created.

    ### Stored Source Metadata
    Versioned stack configs include a top-level `x-files` list with base64-encoded source material for recovery and auditing. This includes the original compose file as `compose.yml`, a generated `.env` containing referenced non-secret environment values, and config files referenced by `configs.*.file` or `configs.*.x-template-file`. Secret source files and variables used by `secrets.*.environment` are not stored in `x-files`.

    ### `x-generate`: Dynamic Secret Generation (Secrets Only)
    This powerful feature allows you to automatically generate random secrets based on specified criteria, eliminating the need to manually create and manage them. This is particularly useful for passwords, API keys, and other sensitive data.

    Supported `x-generate` forms:

    -   `true`
        Generate a secret with default options.
    -   integer
        Generate a secret with the requested length.
    -   object
        Generate a secret with explicit generation flags.

    Supported object flags:

    -   `length`
        Exact secret length.
    -   `numbers`
        Include digits `0-9`.
    -   `special`
        Include special characters.
    -   `uppercase`
        Include uppercase letters `A-Z`.

    Behavior notes:

    -   Generated values are created at deploy time.
    -   Generated secrets are versioned like other managed secrets.
    -   Newly generated values can be shown after deploy when `--show-generated` is enabled.
    -   `x-generate` is for secrets only; configs should use `x-content`, `x-template`, or `x-template-file`.

    -   **Simple Generation (12-20 characters, default options):**
        ```yaml
        secrets:
          my_simple_generated_secret:
            x-generate: true
        ```

    -   **Specify Length:**
        ```yaml
        secrets:
          my_fixed_length_secret:
            x-generate: 30 # Generates a 30-character secret
        ```

    -   **Custom Generation Options:**
        You can provide a dictionary to fine-tune the generation process:
        -   `length`: (integer, default: 12-20 random) Exact length of the secret.
        -   `numbers`: (boolean, default: `true`) Include numbers (0-9).
        -   `special`: (boolean, default: `true`) Include special characters (!@#$%^&*...).
        -   `uppercase`: (boolean, default: `true`) Include uppercase letters (A-Z).

        ```yaml
        secrets:
          my_complex_generated_secret:
            x-generate:
              length: 25
              numbers: false
              special: true
              uppercase: true
          my_alphanumeric_secret:
            x-generate:
              length: 15
              numbers: true
              special: false
              uppercase: false
        ```

    -   **Database Password Style Secret:**
        Generates a strong password with uppercase letters, lowercase letters, numbers, and special characters.
        ```yaml
        secrets:
          db_password:
            x-generate:
              length: 32
              numbers: true
              special: false
              uppercase: true
        ```

    -   **Application Token Without Special Characters:**
        Useful when the target application rejects punctuation in credentials or tokens.
        ```yaml
        secrets:
          app_token:
            x-generate:
              length: 40
              numbers: true
              special: false
              uppercase: true
        ```

    -   **Lowercase Alphanumeric Secret:**
        Useful for systems that want URL-safe or copy-friendly generated values.
        ```yaml
        secrets:
          compact_secret:
            x-generate:
              length: 24
              numbers: true
              special: false
              uppercase: false
        ```

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
