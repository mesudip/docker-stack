#!/usr/bin/python3
"""
NAME
       envsubst.py - substitutes environment variables in bash format strings

DESCRIPTION
    envsubst.py is an upgrade of the POSIX command `envsubst`

    supported syntax:
      normal       - ${VARIABLE1} or $VARIABLE1
      with default - ${VARIABLE1:-somevalue}
"""

import os
import re
import sys


def envsubst(template_str, env=os.environ):
    """Substitute environment variables in the template string, supporting default values."""

    # Regex for ${VARIABLE} with optional default
    pattern_with_default = re.compile(r"\$\{([^}:\s]+)(?::-(.*?))?\}")

    # Regex for $VARIABLE without default
    pattern_without_default = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)")

    def replace_with_default(match):
        var = match.group(1)
        default_value = match.group(2) if match.group(2) is not None else None
        result = env.get(var, default_value)
        if result is None:
            print(f"Missing template variable with default: {var}", file=sys.stderr)
            exit(1)
        return result

    def replace_without_default(match):
        var = match.group(1)
        result = env.get(var, None)
        if result is None:
            print(f"Missing template variable: {var}", file=sys.stderr)
            exit(1)
        return result

    # Substitute variables with default values
    template_str = pattern_with_default.sub(replace_with_default, template_str)

    # Substitute variables without default values
    template_str = pattern_without_default.sub(replace_without_default, template_str)

    return template_str


def main():
    if len(sys.argv) > 2:
        print("Usage: python envsubst.py [template_file]")
        sys.exit(1)

    if len(sys.argv) == 2:
        template_file = sys.argv[1]
        with open(template_file, "r") as file:
            template_str = file.read()
    else:
        template_str = sys.stdin.read()

    result = envsubst(template_str)

    print(result)


if __name__ == "__main__":
    main()
