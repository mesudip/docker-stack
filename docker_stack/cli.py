#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path
from typing import List
from docker_stack import DockerConfig, DockerSecret
import os
import yaml
import json
from docker_stack.docker_objects import DockerObjectManager
from docker_stack.helpers import Command
from docker_stack.registry import DockerRegistry
from .envsubst import envsubst

class Docker:
    def __init__(self,resgistry_url='https://docker.io',userpass=''):
        self.stack = DockerStack(self)
        self.config = DockerConfig()
        self.secret = DockerSecret()
        self.registry = DockerRegistry(resgistry_url, userpass)

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
        
    def render_compose_file(self, compose_file):
        """
        Render the Docker Compose file with environment variables and create Docker configs/secrets.
        """
        with open(compose_file) as f:
            template_content = f.read()
        rendered_content = envsubst(template_content)

        # Parse the YAML content
        compose_data = yaml.safe_load(rendered_content)

        # Process configs and secrets with x-content
        if "configs" in compose_data:
            compose_data["configs"] = self._process_x_content(compose_data["configs"], self.docker.config)
        if "secrets" in compose_data:
            compose_data["secrets"] = self._process_x_content(compose_data["secrets"], self.docker.secret)

        # Convert the modified data back to YAML
        
        rendered_content = yaml.dump(compose_data)

        # Write the rendered file
        rendered_filename = Path(compose_file).with_name(
            f"{Path(compose_file).stem}-rendered{Path(compose_file).suffix}"
        )
        with open(rendered_filename, "w") as f:
            f.write(rendered_content)
        with open(rendered_filename.as_posix()+".json","w") as f:
            f.write(json.dumps(compose_data,indent=2))
        return (rendered_filename,rendered_content)



    def _process_x_content(self, objects, manager:DockerObjectManager):
        """
        Process configs or secrets with x-content keys.
        Returns a tuple: (processed_objects, commands)
        """
        processed_objects = {}
        for name, details in objects.items():
            if isinstance(details, dict) and "x-content" in details:
                # Create the Docker object (config or secret)
                (object_name,command)=manager.create(name, details['x-content'])                
                if not command.isNop():
                    self.commands.append(command)
                # Replace x-content with the name of the created object
                processed_objects[name] = {"name": object_name,"external": True}
            else:
                processed_objects[name] = details
        return processed_objects

    def deploy(self, stack_name, compose_file, with_registry_auth=False):
        rendered_filename, rendered_content = self.render_compose_file(compose_file)
        _, cmd = self.docker.config.increment(stack_name, rendered_content, [f"mesudip.stack.name={stack_name}"])
        if not cmd.isNop():
            self.commands.append(cmd)
        cmd = ["docker", "stack", "deploy", "-c", str(rendered_filename), stack_name]
        if with_registry_auth:
            cmd.insert(3, "--with-registry-auth")
        self.commands.append(Command(cmd))

    def push(self, compose_file, credentials):
        with open(compose_file) as f:
            compose_data = yaml.safe_load(f)
        for service_name, service_data in compose_data.get("services", {}).items():
            if "build" in service_data:
                build_path = service_data["build"]
                print(f"++ docker build -t {service_data['image']} {build_path}")
                build_command = ["docker", "build", "-t", service_data['image'], build_path.get('context', '.')]
                self.commands.append(Command(build_command))
                push_result = self.check_and_push_pull_image(service_data['image'], 'push')
                if push_result:
                    self.commands.append(push_result)
                else:
                    print("No need to push: Already exists")

    def check_and_push_pull_image(self, image_name: str, action: str):
        if self.docker.registry.check_image(image_name):
            print(f"Image {image_name} already in the registry.")
            return None
        if action == 'push':
            print(f"Pushing image {image_name} to the registry...")
            cmd = self.docker.registry.push(image_name)
            if cmd:
                self.commands.append(cmd)


def main():
    parser = argparse.ArgumentParser(description="Deploy and manage Docker stacks.")
    parser.add_argument("command", choices=["deploy", "push"], help="Command to execute")
    parser.add_argument("stack_name", help="Name of the stack", nargs="?")
    parser.add_argument("compose_file", help="Path to the compose file")
    parser.add_argument("--with-registry-auth", action="store_true", help="Use registry authentication")
    parser.add_argument("-u", "--user", help="Registry credentials in format username:password", required=False)

    args = parser.parse_args()
    docker = Docker(resgistry_url="https://registry.sireto.io",userpass='admin:69a017f5de7509e5e7ab0e89a5687dbda58f4fa70762bee17d2e454704bd7a4f')
    docker.load_env()
    docker.check_env()
    docker.registry.login()

    if args.command == "push":
        docker.stack.push(args.compose_file, args.user)
    else:
        docker.stack.deploy(args.stack_name, args.compose_file, args.with_registry_auth)

    print("Commands to Execute")
    [print("   >", x) for x in docker.stack.commands] if docker.stack.commands else print("-- empty --")
    [x.execute() for x in docker.stack.commands]

if __name__ == "__main__":
    main()
