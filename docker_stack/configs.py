import subprocess
import hashlib
import re
import json


def create_config(config_name, config_content, labels=""):
    """
    Creates a Docker config from the given string content.
    Adds a label with the SHA256 hash of the config content, as well as any additional labels.
    
    :param config_name: The name for the Docker config.
    :param config_content: The content of the config as a string.
    :param labels: Additional labels to apply to the Docker config (optional).
    :return: The name of the created Docker config.
    """
    # Generate SHA256 hash of the config content
    sha_hash = hashlib.sha256(config_content.encode('utf-8')).hexdigest()
    
    # Run Docker command to create the config
    command = ["docker", "config", "create"]
    
    # Add the SHA256 label
    command.append("--label")
    command.append(f"sha256={sha_hash}")
    
    # Add any additional labels
    if labels:
        # Split the provided labels into individual ones and add each as a separate --label flag
        for label in labels.split(','):
            command.append("--label")
            command.append(label.strip())
    
    # Add the config name and pass config content via stdin
    command.append(config_name)
    command.append("-")
    
    # Execute the command and pass config_content via stdin
    subprocess.run(command, input=config_content.encode('utf-8'), check=True)
    
    return config_name





def check_config(config_name):
    """
    Checks if a Docker config with the given name already exists.
    
    :param config_name: The name of the config to check.
    :return: True if the config exists, False otherwise.
    """
    command = ["docker", "config", "inspect", config_name]
    
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError:
        return False
def update_config(config_name, config_content):
    """
    Updates an existing Docker config or creates a new one if it doesn't exist.
    If the config exists with a different SHA hash, creates a new version with incremented suffix (_vXX).
    Adds version and original name as labels (mesudip.config.version, mesudip.config.name).
    
    :param config_name: The name of the Docker config.
    :param config_content: The content of the config as a string.
    :return: The name of the updated or newly created Docker config.
    """
    # Calculate the SHA256 hash of the new config content
    sha_hash = hashlib.sha256(config_content.encode('utf-8')).hexdigest()
    
    # Check if the config exists
    if check_config(config_name):
        # Get all configs with the label mesudip.config.name=<config_name> in JSON format
        command = ["docker", "config", "ls", "--filter", f"label=mesudip.config.name={config_name}", "--format", "{{json .}}"]
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = result.stdout.decode()

        # Parse the JSON output
        existing_versions = []
        for line in output.splitlines():
            config_info = json.loads(line)  # Each line is a JSON object
            config_name_in_docker = config_info["Name"]
            # Find version suffix (_vXX)
            match = re.search(r'_(v\d{2})$', config_name_in_docker)
            if match:
                version = int(match.group(1)[1:])  # Extract the version number
                existing_versions.append(version)

        # Find the highest existing version or start from 1 if none exist
        if existing_versions:
            new_version = max(existing_versions) + 1
        else:
            new_version = 2  # Start from v02 if no versions exist
        
        # Create the new config name with the incremented version
        new_version_str = f"_v{new_version:02d}"
        new_config_name = f"{config_name}{new_version_str}"

        # Now, check the SHA256 hash for the existing config with the label "mesudip.config.name"
        command = ["docker", "config", "ls", "--filter", f"label=mesudip.config.name={config_name}", "--format", "{{json .}}"]
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = result.stdout.decode()

        existing_sha_hash = None
        for line in output.splitlines():
            config_info = json.loads(line)  # Parse each config as JSON
            config_name_in_docker = config_info["Name"]
            # Get SHA256 label from the config
            command = ["docker", "config", "inspect", config_name_in_docker, "--format", "{{json .Spec.Labels}}"]
            inspect_result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            labels_info = json.loads(inspect_result.stdout.decode())
            config_sha_hash = labels_info.get("sha256")

            # Check if the SHA256 hash matches the new config's SHA hash
            if config_sha_hash == sha_hash:
                existing_sha_hash = sha_hash
                break
        
        if existing_sha_hash == sha_hash:
            print(f"Config {config_name} already exists with the same SHA hash. No update needed.")
            return config_name  # No need to create a new version if SHA hash matches

        print(f"SHA mismatch. Creating a new version: {new_config_name}")
        # Add labels for version and original name
        labels = f"mesudip.config.version={new_version:02d},mesudip.config.name={config_name}"
        return create_config(new_config_name, config_content, labels)

    else:
        # If the config doesn’t exist, create a new one with _v01
        print(f"Config {config_name} does not exist. Creating new config: {config_name}_v01")
        labels = f"mesudip.config.version=01,mesudip.config.name={config_name}"
        return create_config(config_name, config_content, labels)

if __name__ == '__main__':
    update_config("sudip","sudip4")