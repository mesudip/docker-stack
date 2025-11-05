# Docker Stack CLI Utility

A powerful command-line utility designed to enhance Docker Swarm stack deployments by providing advanced features for managing Docker configs and secrets. This tool aims to simplify complex deployment scenarios and offer capabilities beyond vanilla Docker Stack.

## Features

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

    ### `x-generate`: Dynamic Secret Generation (Secrets Only)
    This powerful feature allows you to automatically generate random secrets based on specified criteria, eliminating the need to manually create and manage them. This is particularly useful for passwords, API keys, and other sensitive data.

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

-   **Docker Stack Versioning and Config Backup for Rollback:**
    The utility automatically versions your Docker configs and secrets, allowing for easy tracking of changes and seamless rollbacks to previous states. This provides a safety net for your deployments, ensuring you can always revert to a stable configuration.

## Why use Docker Stack CLI Utility?

Vanilla Docker Stack deployments can sometimes lack the flexibility needed for dynamic environments or robust secret management. This utility bridges those gaps by:
-   **Automating Secret Management:** No more manual secret generation or complex external scripts.
-   **Simplifying Configuration:** Define configs and secrets directly in your compose files or use templates.
-   **Enhancing Security:** Generate strong, random secrets on the fly.
-   **Enabling Rollbacks:** Versioning ensures you can always revert to a known good state.

Get started today and streamline your Docker Swarm deployments!
