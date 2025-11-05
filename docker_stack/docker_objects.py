import base64
import hashlib
import re
import json
from typing import Dict, List, Tuple, Optional
from docker_stack.helpers import Command, run_cli_command  # Import the helper function


class DockerObjectManager:
    def __init__(self, object_type, log=False):
        self.object_type = object_type
        self.log = log

    def _create(self, object_name, object_content, labels: List[str] = [], sha_hash=None):
        sha_hash = sha_hash if sha_hash else self.calculate_hash(object_name, object_content)
        command = ["docker", self.object_type, "create"]
        command.append("--label")
        command.append(f"sha256={sha_hash}")

        for label in labels:
            command.append("--label")
            command.append(label.strip())

        command.append(object_name)
        command.append("-")

        # Return a Command object with the object_content as stdin
        return Command(command, stdin=object_content, id=sha_hash)

    def calculate_hash(self, object_name, object_content):
        hash_input = f"{self.object_type}{object_name}{object_content}"
        return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    def check(self, object_name):
        command = ["docker", self.object_type, "inspect", object_name]

        try:
            # Use run_cli_command instead of subprocess.run
            run_cli_command(command, raise_error=True, log=self.log)
            return True
        except Exception:
            return False

    def create(self, object_name, object_content, labels: List[str] = [], stack=None) -> Tuple[str, Command]:
        sha_hash = self.calculate_hash(object_name, object_content)
        if stack:
            labels = labels + ["com.docker.stack.namespace=" + stack]

        # Check if any version of the object already exists by its label
        command = [
            "docker",
            self.object_type,
            "ls",
            "--filter",
            f"label=mesudip.object.name={object_name}",
            "--format",
            "{{json .}}",
        ]
        output = run_cli_command(command, raise_error=True, log=self.log)

        # Parse existing versions
        existing_versions = {}
        max_version = 0
        last_object_info: Optional[Dict] = None

        for line in output.splitlines():
            object_info = json.loads(line)
            object_name_in_docker = object_info["Name"]
            if object_name_in_docker == object_name:
                current_version = 1
            else:
                match = re.search(r"_(v\d+)$", object_name_in_docker)
                current_version = int(match.group(1)[1:]) if match else 0

            if current_version > max_version:
                max_version = current_version
                last_object_info = object_info
            existing_versions[current_version] = object_info

        # Determine if the new object has the 'generated' flag
        new_object_is_generated = "mesudip.secret.generated=true" in labels

        if max_version > 0 and self.object_type == "secret":
            # Retrieve labels of the last existing object
            last_object_labels_str = last_object_info["Labels"]
            last_object_labels = parse_labels(last_object_labels_str)
            last_object_is_generated = last_object_labels.get("mesudip.secret.generated") == "true"

            # Case 2: Generated -> Generated. If there is "generated" flag in the last latest version and this new create command also has "generated" flag, return the same old one
            if last_object_is_generated and new_object_is_generated:
                if self.log:
                    print(f"Secret {object_name} was previously generated. Reusing existing secret: {last_object_info['Name']}")
                return last_object_info["Name"], Command.nop

        # Determine the next version number
        new_version_suffix = ""
        if max_version > 0:
            new_version = max_version + 1
            new_version_suffix = f"_v{new_version}"
        else:
            new_version = 1

        new_object_name = f"{object_name}{new_version_suffix}"

        # Check if the SHA hash for the new content already exists in any version
        # This check is still relevant for non-generated secrets or when a generated secret is being overridden
        existing_sha_hash = None
        matching_object = None

        for object_info in existing_versions.values():
            parsed_labels = parse_labels(object_info["Labels"])
            object_sha_hash = parsed_labels.get("sha256")

            if object_sha_hash == sha_hash:
                existing_sha_hash = sha_hash
                matching_object = object_info
                break

        if existing_sha_hash == sha_hash:
            existing_name = matching_object["Name"]
            if self.log:
                print(f"{self.object_type.capitalize()} {existing_name} already exists with the same SHA hash. No update needed.")
            return existing_name, Command.nop

        if self.log:
            print(f"SHA mismatch or new generation. Creating a new version: {new_object_name}")
        labels = [
            f"mesudip.object.version={new_version:01d}",
            f"mesudip.object.name={object_name}",
        ] + labels
        return new_object_name, self._create(new_object_name, object_content, labels, sha_hash=sha_hash)

    def increment(self, object_name, object_content, labels: List[str] = [], stack=None) -> Tuple[str, Command]:
        sha_hash = self.calculate_hash(object_name, object_content)
        if stack:
            labels = labels + ["com.docker.stack.namespace=" + stack]
        # Check if any version of the object already exists by its label
        command = [
            "docker",
            self.object_type,
            "ls",
            "--filter",
            f"label=mesudip.object.name={object_name}",
            "--format",
            "{{json .}}",
        ]
        output = run_cli_command(command, raise_error=True, log=self.log)

        # Parse existing versions
        existing_versions = {}
        max_version = 0

        for line in output.splitlines():
            object_info = json.loads(line)
            object_name_in_docker = object_info["Name"]
            if object_name_in_docker == object_name:
                max_version = max(max_version, 1)
                existing_versions[1] = object_info
            else:
                match = re.search(r"_(v\d+)$", object_name_in_docker)
                if match:
                    version = int(match.group(1)[1:])
                    max_version = max(version, max_version)
                    existing_versions[version] = object_info

        # Determine the next version number
        new_version_suffix = ""
        if len(existing_versions) > 0:
            new_version = max_version + 1
            new_version_suffix = f"_v{new_version}"
        else:
            new_version = 1

        new_object_name = f"{object_name}{new_version_suffix}"
        last_sha = None
        if max_version > 0:
            last_object = existing_versions[max_version]
            command = [
                "docker",
                self.object_type,
                "inspect",
                last_object["Name"],
                "--format",
                "{{json .Spec.Labels}}",
            ]
            inspect_result = run_cli_command(command, raise_error=True, log=self.log)
            labels_info = json.loads(inspect_result)
            last_sha = labels_info.get("sha256")

        if sha_hash == last_sha:
            return (last_object["Name"], Command.nop)
        else:
            labels = [
                f"mesudip.object.version={new_version:01d}",
                f"mesudip.object.name={object_name}",
            ] + labels
            return new_object_name, self._create(new_object_name, object_content, labels, sha_hash=sha_hash)

    def prune(self, keep: int) -> List[Command]:
        """
        Removes old versions of Docker objects (configs or secrets), keeping only the 'keep' most recent.

        Args:
            keep: The number of most recent versions to keep for each object name.

        Returns:
            A list of Command objects for deleting the old versions.
        """
        deletion_commands: List[Command] = []

        # List all objects of this type
        command = [
            "docker",
            self.object_type,
            "ls",
            "--filter",
            "label=mesudip.object.name",  # Filter for objects managed by docker-stack
            "--format",
            "{{json .}}",
        ]
        output = run_cli_command(command, raise_error=True, log=self.log)

        # Group objects by their base name
        objects_by_name: Dict[str, List[Dict]] = {}
        for line in output.splitlines():
            object_info = json.loads(line)
            parsed_labels = parse_labels(object_info["Labels"])
            base_name = parsed_labels.get("mesudip.object.name")
            version = int(parsed_labels.get("mesudip.object.version", 0))

            if base_name and version > 0:
                if base_name not in objects_by_name:
                    objects_by_name[base_name] = []
                objects_by_name[base_name].append({"name": object_info["Name"], "version": version, "id": object_info["ID"]})

        for base_name, versions_list in objects_by_name.items():
            # Sort versions in descending order
            versions_list.sort(key=lambda x: x["version"], reverse=True)

            # Identify objects to delete
            to_delete = versions_list[keep:]

            if to_delete:
                names_to_delete = [obj["name"] for obj in to_delete]
                if self.log:
                    print(f"Pruning {self.object_type}s: {', '.join(names_to_delete)}")
                deletion_commands.append(Command(["docker", self.object_type, "rm"] + names_to_delete))
        return deletion_commands


class DockerConfig(DockerObjectManager):
    def __init__(self, log=False):
        super().__init__("config", log)


class DockerSecret(DockerObjectManager):
    def __init__(self, log=False):
        super().__init__("secret", log)


def parse_labels(label_string):
    """
    Parse a comma-separated string of key-value pairs into a dictionary.
    Handles cases where values may contain commas.

    Args:
        label_string (str): A string containing key-value pairs separated by commas.

    Returns:
        dict: A dictionary of labels and their corresponding values.
    """
    labels = {}
    # Use a regex to split on commas that are not part of a value
    pattern = re.compile(r",(?![^=,]*(?:,|$))")
    for pair in pattern.split(label_string):
        if "=" in pair:
            key, value = pair.split("=", 1)  # Split on the first '=' only
            labels[key.strip()] = value.strip()
    return labels
