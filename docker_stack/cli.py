#!/usr/bin/env python3
import argparse
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
        
    def render_compose_file(self, compose_file,stack=None):
        """
        Render the Docker Compose file with environment variables and create Docker configs/secrets.
        """
        with open(compose_file) as f:
            template_content = f.read()
        base_dir = os.path.dirname(os.path.abspath(compose_file))
        # Parse the YAML content
        compose_data = yaml.safe_load(template_content)

        # Process configs and secrets with x-content
        if "configs" in compose_data:
            compose_data["configs"] = self._process_x_content(compose_data["configs"], self.docker.config,base_dir=base_dir,stack=stack)
        if "secrets" in compose_data:
            compose_data["secrets"] = self._process_x_content(compose_data["secrets"], self.docker.secret,base_dir=base_dir,stack=stack)

        # Convert the modified data back to YAML
        rendered_content = envsubst(yaml.dump(compose_data))

        # Write the rendered file
        rendered_filename = Path(compose_file).with_name(
            f"{Path(compose_file).stem}-rendered{Path(compose_file).suffix}"
        )
        with open(rendered_filename, "w") as f:
            f.write(rendered_content)
        with open(rendered_filename.as_posix()+".json","w") as f:
            f.write(json.dumps(compose_data,indent=2))
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

    def deploy(self, stack_name, compose_file, with_registry_auth=False):
        rendered_filename, rendered_content = self.render_compose_file(compose_file,stack=stack_name)
        _, cmd = self.docker.config.increment(stack_name, rendered_content, [f"mesudip.stack.name={stack_name}"],stack=stack_name)
        if not cmd.isNop():
            self.commands.append(cmd)
        cmd = ["docker", "stack", "deploy", "-c", str(rendered_filename), stack_name]
        if with_registry_auth:
            cmd.insert(3, "--with-registry-auth")
        self.commands.append(Command(cmd,give_console=True))

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


def main(args:List[str]=None):
    parser = argparse.ArgumentParser(description="Deploy and manage Docker stacks.")
    parser.add_argument("command", choices=["deploy", "push"], help="Command to execute")
    parser.add_argument("stack_name", help="Name of the stack", nargs="?")
    parser.add_argument("compose_file", help="Path to the compose file")
    parser.add_argument("--with-registry-auth", action="store_true", help="Use registry authentication")
    parser.add_argument("-u", "--user", help="Registry credentials in format hostname:username:password",action='append', required=False,default=[])
    parser.add_argument("-t", "--tag", help="Tag the current deployment for later checkout", required=False)


    args = parser.parse_args(args=(args if args else sys.argv[1:]))
    docker = Docker(registries=args.user)
    docker.load_env()
    docker.check_env()

    if args.command == "push":
        docker.stack.push(args.compose_file, args.user)
    else:
        docker.stack.deploy(args.stack_name, args.compose_file, args.with_registry_auth)

    # [x.execute() for x in docker.stack.commands]

if __name__ == "__main__":
    main([""])
