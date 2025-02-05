#!/usr/bin/python3
"""
NAME
       envsubst_merge.py - merges files and substitutes environment variables in the content

DESCRIPTION
    envsubst_merge.py combines the functionality of merging files and substituting environment
    variables in bash format strings. An optional file extension can be provided to filter the files to be merged.
"""

import os
import re
import sys


def envsubst(template_str, env=os.environ):
    """Substitute environment variables in the template string, supporting default values."""

    # Regex for ${VARIABLE} with optional default
    pattern_with_default = re.compile(r"\$\{([^}:\s]+)(?::-([^}]*))?\}")

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


def merge_files_from_directories(directories, file_extension=None):
    merged_content = []

    for path in directories:
        if os.path.isdir(path):
            # If the path is a directory, read files with the specified extension
            for filename in os.listdir(path):
                if not file_extension or filename.endswith(file_extension):
                    filepath = os.path.join(path, filename)
                    with open(filepath, "r") as file:
                        content = (
                            file.read().strip()
                        )  # Strip leading/trailing whitespace
                        if content:  # Add only non-empty content
                            # Add directory and filename as a comment at the start of the content
                            merged_content.append(f"# {path}/{filename}\n{content}")
        elif os.path.isfile(path) and (
            not file_extension or path.endswith(file_extension)
        ):
            # If the path is a file with the specified extension, read its content
            with open(path, "r") as file:
                content = file.read().strip()  # Strip leading/trailing whitespace
                if content:  # Add only non-empty content
                    # Add the file name as a comment at the start of the content
                    merged_content.append(f"# {path}\n{content}")
        else:
            print(
                f"Warning: '{path}' is not a valid directory or file. Skipping.",
                file=sys.stderr,
            )

    # Join the content with a single newline between entries
    result = "\n\n".join(merged_content)

    # Strip extra empty lines from the beginning and end
    result = result.strip()

    # Perform environment variable substitution on the final result
    return envsubst(result)


def main():
    # Take directories or files from command-line arguments
    if len(sys.argv) < 2:
        print(
            "Usage: python envsubst_merge.py <directory1|file1> <directory2|file2> ... [--ext <file_extension>] [--ext=<file_extension>]",
            file=sys.stderr,
        )
        sys.exit(1)

    paths_to_read = []
    file_extension = None

    # Parse arguments
    args = iter(sys.argv[1:])
    for arg in args:
        if arg.startswith("--ext="):
            file_extension = arg.split("=", 1)[1]
        elif arg == "--ext":
            try:
                file_extension = next(args)
            except StopIteration:
                print("Error: Missing file extension after --ext.", file=sys.stderr)
                sys.exit(1)
        else:
            paths_to_read.append(arg)

    result = merge_files_from_directories(paths_to_read, file_extension)

    # Print the output to stdout
    print(result)


if __name__ == "__main__":
    main()
