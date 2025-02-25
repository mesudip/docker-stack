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

    template_str = template_str.replace("$$", "__ESCAPED_DOLLAR__")
    def print_error_line(template_str, match_span):
        """Helper function to print the error context."""
        lines = template_str.splitlines()
        
        # Determine the start position and line
        start_pos = match_span[0]
        end_pos = match_span[1]

        # Calculate line numbers based on character positions
        char_count = 0
        start_line = end_line = None
        for i, line in enumerate(lines):
            char_count += len(line) + 1  # +1 for the newline character
            if start_line is None and char_count > start_pos:
                start_line = i
            if char_count >= end_pos:
                end_line = i
                break
        
        # Display lines before, the error line, and after (with line numbers)
        start = max(start_line - 1, 0)
        end = min(end_line + 1, len(lines) - 1)

        for i in range(start, end + 1):
            print(f"{i + 1}: {lines[i]}",file=sys.stderr)

    def replace_with_default(match: re.Match[str]):
        var = match.group(1)
        default_value = match.group(2) if match.group(2) is not None else None
        result = env.get(var, default_value)
        if result is None:
            print_error_line(template_str, match.span())
            print(f"ERROR :: Missing template variable with default: {var}", file=sys.stderr)

            exit(1)
        return result

    def replace_without_default(match: re.Match[str]):
        var = match.group(1)
        result = env.get(var, None)
        if result is None:
            print_error_line(template_str, match.span())
            print(f"ERROR :: Missing template variable: {var}", file=sys.stderr)
            exit(1)
        return result

    # Substitute variables with default values
    template_str = pattern_with_default.sub(replace_with_default, template_str)

    # Substitute variables without default values
    template_str = pattern_without_default.sub(replace_without_default, template_str)
    
    template_str = template_str.replace("__ESCAPED_DOLLAR__", "$")

    return template_str


def envsubst_load_file(template_file,env=os.environ):
    with open(template_file) as file:
        return envsubst(file.read(),env)

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

