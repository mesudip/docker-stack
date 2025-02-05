import os
import sys


def merge_files_from_directories(directories):
    merged_content = []

    for path in directories:
        if os.path.isdir(path):
            # If the path is a directory, read .conf files from it
            for filename in os.listdir(path):
                if filename.endswith(".conf"):  # Only consider .conf files
                    filepath = os.path.join(path, filename)
                    with open(filepath, "r") as file:
                        content = (
                            file.read().strip()
                        )  # Strip leading/trailing whitespace
                        if content:  # Add only non-empty content
                            # Add directory and filename as a comment at the start of the content
                            merged_content.append(f"# {path}/{filename}\n{content}")
        elif os.path.isfile(path) and path.endswith(".conf"):
            # If the path is a .conf file, read its content
            with open(path, "r") as file:
                content = file.read().strip()  # Strip leading/trailing whitespace
                if content:  # Add only non-empty content
                    # Add the file name as a comment at the start of the content
                    merged_content.append(f"# {path}\n{content}")
        else:
            print(
                f"Warning: '{path}' is not a valid directory or .conf file. Skipping."
            )

    # Join the content with a single newline between entries
    result = "\n\n".join(merged_content)

    # Strip extra empty lines from the beginning and end
    result = result.strip()

    # Print the output to stdout
    print(result)


if __name__ == "__main__":
    # Take directories or .conf files from command-line arguments
    if len(sys.argv) < 2:
        print(
            "Usage: python merge_files.py <directory1|file1.conf> <directory2|file2.conf> ..."
        )
        sys.exit(1)

    paths_to_read = sys.argv[1:]
    merge_files_from_directories(paths_to_read)
