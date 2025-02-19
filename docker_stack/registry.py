import json
import http.client
import select
import subprocess
from base64 import b64encode
from typing import Dict, List

from docker_stack.helpers import Command
from docker_stack.url_parser import ConnectionDetails, parse_url
import os
class DockerRegistry:
    def __init__(self, _registries:List[str]):
        """
        Initializes the DockerRegistry class with the registry URL, and optional
        username and password for authentication.
        
        :param registry_url: URL of the Docker registry (e.g., 'registry.hub.docker.com')
        :param username: Optional username for authentication
        :param password: Optional password for authentication
        """
        registries = [ parse_url(registry) for registry in _registries]
        for registry in registries:
            splitted=registry["host"].split(':')
            if len(splitted)> 1:
                if splitted[1] =='443':
                    registry['host']=splitted[0]
                    registry["scheme"] = "https"
        
        self.registries:Dict[str,ConnectionDetails] = {x["host"]:x for x in self.load_system_connections()} 
        for reg in registries:
            self.registries[reg["host"]]=reg
            
        self.authenticated=set()

    def load_system_connections(self) -> List[ConnectionDetails]:
        # Determine the path to the Docker config file
        home_dir = os.getenv('HOME', '/root')
        config_path = os.path.join(home_dir, '.docker', 'config.json')

        # Initialize an empty list to store connection details
        connections = []

        try:
            # Open and read the Docker config file
            with open(config_path, 'r') as file:
                config = json.load(file)

                # Extract the 'auths' section
                auths = config.get('auths', {})
                for host, auth_info in auths.items():
                    # Extract the auth token (username:password in base64)
                    auth_token = auth_info.get('auth', '')
                    if auth_token:
                        # Decode the base64 token to get username:password
                        import base64
                        decoded_token = base64.b64decode(auth_token).decode('utf-8')
                        username, password = decoded_token.split(':', 1)

                        # Create a ConnectionDetails dictionary
                        connection: ConnectionDetails = {
                            'scheme': 'https',  # Docker registries typically use HTTPS
                            'host': host,
                            'username': username,
                            'password': password
                        }
                        connections.append(connection)

        except FileNotFoundError:
            print("[Docker Config Load] Docker config file not found.")
        except json.JSONDecodeError:
            print("[Docker Config Load] Invalid JSON in Docker config file.")
        except Exception as e:
            print(f"[Docker Config Load] An error occurred: {e}")

        return connections


    def _get_host_from_url(self, url:str):
        """Extracts the host from the URL."""
        # Remove protocol part (http:// or https://)
        if url.startswith('http://') or url.startswith("https://"):
            return url.split('://')[1].split('/')[0]
        return url
    
    def _send_request(self, conn:ConnectionDetails ,method, endpoint)->http.client.HTTPResponse:
        """Send a generic HTTP request to the Docker registry."""
        connection = http.client.HTTPSConnection(conn["host"]) if conn["scheme"]=='https' else http.client.HTTPConnection(conn["host"])
        
        # Add Authorization header if needed
        headers = {}
        if conn["username"]:
            auth_string=conn['username']+':'+conn['password']
            headers['Authorization'] = f"Basic {b64encode(auth_string.encode()).decode()}"
        
        connection.request(method, endpoint, headers=headers)
        response = connection.getresponse()
        return response
    
    def check_auth(self,conn:ConnectionDetails):
        """
        Check if the authentication credentials (if provided) are valid for the Docker registry.
        
        :return: Boolean indicating whether authentication is successful
        """
        url = "/v2/"
        response = self._send_request(conn,'GET', url)
        if response.status == 200:
            self.authenticated.add(conn["host"])
            return True


    def check_image(self, image_name):
        """
        Check if an image exists in the Docker registry.
        
        :param image_name: Name of the image (e.g., 'ubuntu' or 'python')
        :return: Boolean indicating whether the image exists in the registry
        """
        self.login_for_image(image_name)
        hostname=extract_host_from_image_name(image_name)
        url = f"/v2/{image_name}/tags/list"

        if hostname in self.registries:
            response = self._send_request(self.registries[hostname],'GET',url)
        else:
            registry=parse_url(hostname)
            response =  self._send_request(registry,'GET',url)
        print("response",response.read())
        return response.status == 200
    
    def _run_docker_command(self, command):
        """
        Run a Docker command using the subprocess module and stream the output to the terminal in real-time.
        
        :param command: A list of strings representing the Docker command to run
        :return: None
        """
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

            # Use select to handle both stdout and stderr without blocking
            while process.poll() is None:
                readable, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)
                for stream in readable:
                    line = stream.readline()
                    if line:
                        print(line, end="", flush=True)

            # Ensure remaining output is printed
            for stream in (process.stdout, process.stderr):
                for line in iter(stream.readline, ""):
                    print(line, end="", flush=True)

            process.stdout.close()
            process.stderr.close()
            process.wait()

            if process.returncode != 0:
                print(f"Command failed with return code {process.returncode}")
            
        except FileNotFoundError:
            print("Docker command not found. Please ensure Docker is installed and accessible.")
            
    def _run_docker_command_(self, command):
        """
        Run a Docker command using the subprocess module.
        
        :param command: A list of strings representing the Docker command to run
        :return: Tuple of (stdout, stderr)
        """
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return result.stdout, result.stderr
        except FileNotFoundError:
            return '', 'Docker not found. Please install Docker.'

    def push(self, image_name)->Command:
        """
        Push an image to the Docker registry.
        
        :param image_name: Name of the image to push (e.g., 'myrepo/myimage:tag')
        :return: Tuple of (stdout, stderr)
        """
        self.login_for_image(image_name)
        return Command(['docker', 'push', image_name])

    def pull(self, image_name):
        """
        Pull an image from the Docker registry.
        
        :param image_name: Name of the image to pull (e.g., 'myrepo/myimage:tag')
        :return: Tuple of (stdout, stderr)
        """
        self.login_for_image(image_name)
        command = ['docker', 'pull', image_name]
        return self._run_docker_command(command)
            

    def login_for_image(self,image):
        hostname= extract_host_from_image_name(image)
        if hostname  in self.authenticated:
            return True
        if hostname in self.registries and self.check_auth(self.registries[hostname]):
            self.authenticated.add(hostname)
        else:
            registry=self.registries.get(hostname)
            if registry:
                print("> " ," ".join(['docker','login','-u',registry["username"],'-p','[redacted]',registry["host"]]) )
                subprocess.run(['docker','login','-u',registry['username'],'-p',registry['password'],registry["host"]])
                self.authenticated.add(hostname)  
        return hostname


def extract_host_from_image_name(image_name: str) -> str:
    """
    Extracts the hostname (registry address) from a Docker image name.
    
    :param image_name: Docker image name (e.g., 'ubuntu', 'myregistry.com/myimage', or 'myregistry.com/myrepo/myimage:tag')
    :return: Hostname (e.g., 'docker.io', 'myregistry.com')
    """
    # Remove protocol part (http:// or https://) if present
    if image_name.startswith('http://'):
        image_name = image_name[len('http://'):]
    elif image_name.startswith('https://'):
        image_name = image_name[len('https://'):]

    # Check if the image name has a registry/hostname
    if '/' in image_name:
        parts = image_name.split('/', 1)
        # If it looks like a full URL (e.g., myregistry.com/myimage:tag)
        if '.' in parts[0] and not parts[0].startswith('http'):
            splitted=parts[0].split(':')
            if len(splitted)> 1:
                if splitted[1] =='443':
                    return splitted[0]
            return parts[0]
        # If it looks like a username/repository (e.g., 'username/myimage')
        return 'docker.io'
    return 'docker.io'
