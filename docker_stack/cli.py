#!/usr/bin/env python3
import argparse
import base64
import re
import subprocess
import sys
from pathlib import Path
from typing import List
import os
import yaml
import json
from docker_stack.docker_objects import DockerConfig, DockerObjectManager, DockerSecret
from docker_stack.helpers import Command
from docker_stack.registry import DockerRegistry
from .envsubst import envsubst, envsubst_load_file

class Docker:
    def __init__(self,registries:List[str]=[]):
        self.stack = DockerStack(self)
        self.config = DockerConfig()
        self.secret = DockerSecret()
        self.registry = DockerRegistry(registries)

    @staticmethod
    def load_env(env_file=".env"):
        if Path(env_file).is_file():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        key, _, value = line.partition("=")
                        os.environ[key.strip()] = value.strip()

    @staticmethod
    def check_env(example_file=".env.example"):
        if not Path(example_file).is_file():
            return

        unset_keys = []
        with open(example_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    key = line.split("=")[0].strip()
                    if not os.environ.get(key):
                        unset_keys.append(key)

        if unset_keys:
            print("The following keys are not set in the environment:")
            for key in unset_keys:
                print(f"- {key}")
            print("Exiting due to missing environment variables.")
            sys.exit(2)

class DockerStack:
    def __init__(self, docker: Docker):
        self.docker = docker
        self.commands: List[Command] = []
    
    def read_compose_file(self,compose_file)->dict:
        with open(compose_file) as f:
           return self.decode_yaml(f.read())
    
    def rendered_compose_file(self,compose_file,stack=None,include_build=True)->str:
        with open(compose_file) as f:
            template_content = f.read()
        # Parse the YAML content
        compose_data = self.decode_yaml(template_content)
        if not include_build:
            services: dict=compose_data.get('services',{})
            for (k,v) in services.items():
                if 'build' in v:
                    del v['build']
        if stack:
            base_dir = os.path.dirname(os.path.abspath(compose_file))
            if "configs" in compose_data:
                compose_data["configs"] = self._process_x_content(compose_data["configs"], self.docker.config,base_dir=base_dir,stack=stack)
            if "secrets" in compose_data:
                compose_data["secrets"] = self._process_x_content(compose_data["secrets"], self.docker.secret,base_dir=base_dir,stack=stack)
        return envsubst(yaml.dump(compose_data))
    
    def decode_yaml(self,data:str)->dict:
        return yaml.safe_load(data)
        
    def render_compose_file(self, compose_file,stack=None,include_build=True):
        """
        Render the Docker Compose file with environment variables and create Docker configs/secrets.
        """
        
        # Convert the modified data back to YAML
        rendered_content = self.rendered_compose_file(compose_file,stack,include_build=include_build)

        # Write the rendered file
        rendered_filename = Path(compose_file).with_name(
            f"{Path(compose_file).stem}-rendered{Path(compose_file).suffix}"
        )
        with open(rendered_filename, "w") as f:
            f.write(rendered_content)
        with open(rendered_filename.as_posix()+".json","w") as f:
            f.write(json.dumps(rendered_content,indent=2))
        return (rendered_filename,rendered_content)



    def _process_x_content(self, objects, manager:DockerObjectManager,base_dir="",stack=None):
        """
        Process configs or secrets with x-content keys.
        Returns a tuple: (processed_objects, commands)
        """
        processed_objects = {}
        
        def add_obj(name,data):
            (object_name,command)=manager.create(name, data,stack=stack)                
            if not command.isNop():
                self.commands.append(command)
            processed_objects[name] = {"name": object_name,"external": True}
        for name, details in objects.items():
            if isinstance(details, dict) and "x-content" in details:
                add_obj(name,details['x-content'])
            elif isinstance(details, dict) and 'x-template' in details:
                add_obj(name,envsubst(details['x-content'],os.environ))
            elif isinstance(details, dict) and 'x-template-file' in details:
                filename=os.path.join(base_dir,details['x-template-file'])
                add_obj(name,envsubst_load_file(filename,os.environ))
            elif isinstance(details, dict) and 'file' in details:
                filename=os.path.join(base_dir,details['file'])
                with open(filename) as file:
                    add_obj(name,file.read())
            else:
                processed_objects[name] = details
        return processed_objects
    
    def ls(self):
        cmd = ["docker", "config", "ls", "--format", "{{.ID}}\t{{.Name}}\t{{.Labels}}"]
        output = subprocess.check_output(cmd, text=True).strip().split("\n")
        stack_versions = {}
        
        for line in output:
            parts = line.split("\t")
            if len(parts) == 3 and "mesudip.stack.name" in parts[2]:
                labels = {k: v for k, v in (label.split("=") for label in parts[2].split(",") if "=" in label)}
                stack_name = labels.get("mesudip.stack.name")
                version = labels.get("mesudip.object.version", "unknown")
                
                if stack_name:
                    if stack_name not in stack_versions:
                        stack_versions[stack_name] = []
                    stack_versions[stack_name].append(version)

        # Calculate max stack name width
        max_stack_name_length = max(len(stack) for stack in stack_versions) if stack_versions else 10
        header_stack = "Stack Name".ljust(max_stack_name_length)
        
        print(f"{header_stack} | Versions")
        print("-" * (max_stack_name_length + 12))
        
        for stack, versions in sorted(stack_versions.items()):
            versions_str = ", ".join(sorted(versions, key=int))
            print(f"{stack.ljust(max_stack_name_length)} | {versions_str}")

        return stack_versions

    def cat(self, name:str, version:str):
        if version.startswith('v') or version.startswith('V'):
            version = version[1:]
        if version == '1':
            name = f"{name}"
        else:
            name = f"{name}_v{version}"

        cmd = ["docker", "config", "inspect", name]
        output = subprocess.check_output(cmd, text=True).strip()
        
        # Parse the JSON output
        configs = json.loads(output)
        if not configs:
            print(f"No config found for {name}")
            return None

        # Extract and decode the base64-encoded Spec.Data
        encoded_data = configs[0].get("Spec", {}).get("Data", "")
        if not encoded_data:
            print(f"No data found in config {name}")
            return None

        decoded_data = base64.b64decode(encoded_data).decode("utf-8")        
        return decoded_data

    def versions(self, stack_name):
        cmd = ["docker", "config", "ls", "--format", "{{.Name}}\t{{.Labels}}"]
        output = subprocess.check_output(cmd, text=True).strip().split("\n")
        versions_list = []

        for line in output:
            parts = line.split("\t")
            if len(parts) == 2 and "mesudip.stack.name" in parts[1]:
                labels = {k: v for k, v in (label.split("=") for label in parts[1].split(",") if "=" in label)}
                stack = labels.get("mesudip.stack.name")
                version = labels.get("mesudip.object.version", "unknown")
                tag = labels.get("mesudip.stack.tag", "")

                if stack == stack_name:
                    versions_list.append((version, tag))

        # Add headers to list for proper spacing calculation
        versions_list.insert(0, ("Version", "Tag"))

        # Determine max column widths
        max_version_length = max(len(v[0]) for v in versions_list)
        max_tag_length = max(len(v[1]) for v in versions_list)

        # Print header
        print(f"{'Version'.ljust(max_version_length)} | {'Tag'.ljust(max_tag_length)}")
        print("-" * (max_version_length + max_tag_length + 3))

        # Print sorted versions (excluding header)
        for version, tag in sorted(versions_list[1:], key=lambda x: int(x[0]) if x[0].isdigit() else x[0]):
            print(f"{version.ljust(max_version_length)} | {tag.ljust(max_tag_length)}")

        return versions_list[1:]

    def checkout(self, stack_name, identifier, with_registry_auth=False):
        """
        Deploys a stack by version or tag.
        
        :param stack_name: Name of the stack.
        :param identifier: Version (e.g., 'v1.2', 'V3') or tag (e.g., 'stable', 'latest').
        :param with_registry_auth: Whether to use registry authentication.
        """

        # Regex to check if the identifier is a version (optional "v" or "V" at the start, followed by digits)
        version_pattern = re.compile(r"^[vV]?(\d+(\.\d+)*)$")

        match = version_pattern.match(identifier)
        if match:
            version = match.group(1)  # Extract the numeric version part
            tag = None
        else:
            tag = identifier
            versions_list = self.versions(stack_name)
            matching_versions = [v for v, t in versions_list if t == tag]
            if not matching_versions:
                raise ValueError(f"No version found for tag '{tag}' in stack '{stack_name}'")
            version = matching_versions[0]  # Use the first matching version

        compose_content = self.cat(stack_name, version)
        
        temp_file = f"/tmp/{stack_name}_v{version}.yml"
        with open(temp_file, "w") as f:
            f.write(compose_content)

        print(f"Deploying stack {stack_name} with version {version} (tag: {tag})...")
        self._deploy(stack_name, temp_file, compose_content, with_registry_auth=with_registry_auth, tag=tag)

        
    def _deploy(self, stack_name, rendered_filename,rendered_content, with_registry_auth=False,tag=None):
        labels = [f"mesudip.stack.name={stack_name}"]
        if tag:
            labels.append(f"mesudip.stack.tag={tag}")

        _, cmd = self.docker.config.increment(stack_name, rendered_content,labels=labels ,stack=stack_name)
        if not cmd.isNop():
            self.commands.append(cmd)
        cmd = ["docker", "stack", "deploy", "-c", str(rendered_filename), stack_name]
        if with_registry_auth:
            cmd.insert(3, "--with-registry-auth")
        self.commands.append(Command(cmd,give_console=True))

    def deploy(self, stack_name, compose_file, with_registry_auth=False,tag=None):
        rendered_filename, rendered_content = self.render_compose_file(compose_file,stack=stack_name,include_build=False)
        labels = [f"mesudip.stack.name={stack_name}"]
        if tag:
            labels.append(f"mesudip.stack.tag={tag}")

        _, cmd = self.docker.config.increment(stack_name, rendered_content,labels=labels ,stack=stack_name)
        if not cmd.isNop():
            self.commands.append(cmd)
        cmd = ["docker", "stack", "deploy", "-c", str(rendered_filename), stack_name]
        if with_registry_auth:
            cmd.insert(3, "--with-registry-auth")
        self.commands.append(Command(cmd,give_console=True))

    def push(self, compose_file):
        compose_data = self.read_compose_file(compose_file)
        for service_name, service_data in compose_data.get("services", {}).items():
            if "build" in service_data:
                image= envsubst(service_data['image'])
                push_result = self.check_and_push_pull_image(image, 'push')
                if push_result:
                    self.commands.append(push_result)
                else:
                    # print("No need to push: Already exists")
                    pass

    def build_and_push(self, compose_file: str, push: bool = False) -> None:
        """
        Build Docker images from a Compose file and optionally push them.

        Args:
            compose_file (str): Path to the Docker Compose file.
            push (bool): Whether to push the built images. Defaults to False.
        """
        compose_data = self.read_compose_file(compose_file)
        base_dir = os.path.dirname(os.path.abspath(compose_file))

        for service_name, service_data in compose_data.get("services", {}).items():
            if "build" in service_data:
                build_config = service_data["build"]
                image = envsubst(service_data['image'])

                build_command = ["docker", "build", "-t", image]
              
                              
                args = build_config.get('args', [])

                if isinstance(args, dict):
                    for key, val in args.items():
                        build_command.extend(["--build-arg", f"{envsubst(key)}={envsubst(val)}"])
                elif isinstance(args, list):
                    for value in args:
                        build_command.extend(["--build-arg", envsubst(value)])

                build_command.append(os.path.join(base_dir, build_config.get('context', '.')))
                self.commands.append(Command(build_command))

                if push:
                    push_result = self.check_and_push_pull_image(image, 'push')
                    if push_result:
                        self.commands.append(push_result)
                    else:
                        # print("No need to push: Already exists")
                        pass
    def check_and_push_pull_image(self, image_name: str, action: str):
        if self.docker.registry.check_image(image_name):
            return None
        if action == 'push':
            cmd = self.docker.registry.push(image_name)
            if cmd:
                self.commands.append(cmd)


def main(args:List[str]=None):
    parser = argparse.ArgumentParser(description="Deploy and manage Docker stacks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Build subcommand
    build_parser = subparsers.add_parser("build", help="Build images using docker-compose")
    build_parser.add_argument("compose_file", help="Path to the compose file")
    build_parser.add_argument("--push", action="store_true", help="Use registry authentication")

    # Push subcommand
    push_parser = subparsers.add_parser("push", help="Push images to registry")
    push_parser.add_argument("compose_file", help="Path to the compose file")
    
    # Deploy subcommand
    deploy_parser = subparsers.add_parser("deploy", help="Deploy stack using docker stack deploy")
    deploy_parser.add_argument("stack_name", help="Name of the stack")
    deploy_parser.add_argument("compose_file", help="Path to the compose file")
    deploy_parser.add_argument("--with-registry-auth", action="store_true", help="Use registry authentication")
    deploy_parser.add_argument("-t", "--tag", help="Tag the current deployment for later checkout", required=False)

    # Remove subcommand
    rm_parser = subparsers.add_parser("rm", help="Remove a deployed stack")
    rm_parser.add_argument("stack_name", help="Name of the stack")
    
    # Ls command
    subparsers.add_parser("ls",help="List docker-stacks")
    
    cat_parser = subparsers.add_parser("cat",help="Print the docker compose of specific version")
    cat_parser.add_argument("stack_name", help="Name of the stack")
    cat_parser.add_argument("version", help="Stack version to cat")
    
    checkout_parser = subparsers.add_parser("checkout",help="Deploy specific version of the stack")
    checkout_parser.add_argument("stack_name", help="Name of the stack")
    checkout_parser.add_argument("version", help="Stack version to cat")

    # version_parser = subparsers.add_parser("version",help="Deploy specific version of the stack")
    # version_parser.add_argument("stack_name", help="Name of the stack")
    # version_parser.add_argument("version","versions", help="Stack version to cat")
        
    version_parser = subparsers.add_parser("version",aliases=["versions"], help="Deploy specific version of the stack")
    version_parser.add_argument("stack_name", help="Name of the stack")

    
    parser.add_argument("-u", "--user", help="Registry credentials in format hostname:username:password", action="append", required=False, default=[])
    parser.add_argument("-t", "--tag", help="Tag the current deployment for later checkout", required=False)
    parser.add_argument("-ro",'-r',"--ro","--r", "--dry-run", action="store_true", help="Print commands, don't execute them", required=False)
    
    args = parser.parse_args(args if args else sys.argv[1:])
    
    docker = Docker(registries=args.user)
    docker.load_env()
    
    if args.command == "build":
        docker.stack.build_and_push(args.compose_file,push=args.push)
    elif args.command == "push":
        docker.stack.push(args.compose_file)
    elif args.command == "deploy":
        docker.stack.deploy(args.stack_name, args.compose_file, args.with_registry_auth,tag=args.tag)
    elif args.command == "ls":
        docker.stack.ls()

    elif args.command == "rm":
        docker.stack.rm(args.stack_name)
    elif args.command == 'cat':
        print(docker.stack.cat(args.stack_name,args.version))
    elif args.command == 'checkout':
        docker.stack.checkout(args.stack_name,args.version)
    elif args.command == 'versions' or args.command == "version":
        docker.stack.versions(args.stack_name)
    if args.ro:
        print("Following commands were not executed:")
        [print(" >> "+str(x)) for x in docker.stack.commands if x]
    else:
        [ x.execute() for x in docker.stack.commands]


if __name__ == "__main__":
    main([""])
