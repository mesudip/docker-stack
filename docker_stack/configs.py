import subprocess
import hashlib
import re
import json


def create_config(config_name, config_content, labels=""):
    sha_hash = hashlib.sha256(config_content.encode('utf-8')).hexdigest()
    command = ["docker", "config", "create"]
    command.append("--label")
    command.append(f"sha256={sha_hash}")
    
    if labels:
        for label in labels.split(','):
            command.append("--label")
            command.append(label.strip())

    command.append(config_name)
    command.append("-")
    
    subprocess.run(command, input=config_content.encode('utf-8'), check=True)
    return config_name


def check_config(config_name):
    command = ["docker", "config", "inspect", config_name]
    
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError:
        return False


def update_config(config_name, config_content):
    sha_hash = hashlib.sha256(config_content.encode('utf-8')).hexdigest()
    
    # Check if any version of the config already exists by its label
    command = ["docker", "config", "ls", "--filter", f"label=mesudip.config.name={config_name}", "--format", "{{json .}}"]
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = result.stdout.decode()
    
    # Parse existing versions
    existing_versions = {}
    max_version=0
    
    for line in output.splitlines():
        config_info = json.loads(line)
        config_name_in_docker = config_info["Name"]
        if config_name_in_docker == config_name:
          max_version=max(max_version,1) 
          existing_versions[1]=config_info
        else:     
            match = re.search(r'_(v\d+)$', config_name_in_docker)
            if match:
                version = int(match.group(1)[1:])
                max_version=max(version,max_version)
                existing_versions[version] = config_info

    # Determine the next version number
    new_version_suffix=""
    if len(existing_versions) > 0:
        new_version = max_version + 1
        new_version_suffix=f"_v{new_version}"
    else:
        new_version = 1
    
    new_config_name = f"{config_name}{new_version_suffix}"

    # Check if the SHA hash for the new content already exists in any version
    existing_sha_hash = None
    matching_config=None
    
    for config_info in existing_versions.values():
        config_name_in_docker = config_info["Name"]
        command = ["docker", "config", "inspect", config_name_in_docker, "--format", "{{json .Spec.Labels}}"]
        inspect_result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        labels_info = json.loads(inspect_result.stdout.decode())
        config_sha_hash = labels_info.get("sha256")
        
        if config_sha_hash == sha_hash:
            existing_sha_hash = sha_hash
            matching_config=config_info
            break

    if existing_sha_hash == sha_hash:
        existing_name=matching_config["Name"]
        print(f"Config {existing_name} already exists with the same SHA hash. No update needed.")
        return existing_name

    print(f"SHA mismatch. Creating a new version: {new_config_name}")
    labels = f"mesudip.config.version={new_version:01d},mesudip.config.name={config_name}"
    return create_config(new_config_name, config_content, labels)


if __name__ == '__main__':
    update_config("sudip", "sudip2")
