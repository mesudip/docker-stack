import json
import http.client
import select
import subprocess
from base64 import b64encode

from docker_stack.helpers import Command

class DockerRegistry:
    def __init__(self, registry_url, userpass:str=None):
        """
        Initializes the DockerRegistry class with the registry URL, and optional
        username and password for authentication.
        
        :param registry_url: URL of the Docker registry (e.g., 'registry.hub.docker.com')
        :param username: Optional username for authentication
        :param password: Optional password for authentication
        """
        self.registry_url = registry_url
        self.username = None
        self.password = None
        if userpass:
            splitted=userpass.split(':')
            
            self.username=splitted[0]
            self.password=splitted[1]
       
        # Parse registry host and port
        self.host = self._get_host_from_url(registry_url)
        self.port = 443 if self.registry_url.startswith("https") else 80
    
    def _get_host_from_url(self, url):
        """Extracts the host from the URL."""
        # Remove protocol part (http:// or https://)
        return url.split('://')[1].split('/')[0]
    
    def _send_request(self, method, endpoint, auth=None):
        """Send a generic HTTP request to the Docker registry."""
        connection = http.client.HTTPSConnection(self.host, self.port)
        
        # Add Authorization header if needed
        headers = {}
        if auth:
            headers['Authorization'] = f"Basic {b64encode(auth.encode()).decode()}"
        
        connection.request(method, endpoint, headers=headers)
        response = connection.getresponse()
        return response
    
    def check_auth(self):
        """
        Check if the authentication credentials (if provided) are valid for the Docker registry.
        
        :return: Boolean indicating whether authentication is successful
        """
        url = "/v2/"
        if self.username and self.password:
            auth = f"{self.username}:{self.password}"
            response = self._send_request('GET', url, auth)
        else:
            response = self._send_request('GET', url)

        # Check if the status code is 200
        return response.status == 200

    def check_image(self, image_name):
        """
        Check if an image exists in the Docker registry.
        
        :param image_name: Name of the image (e.g., 'ubuntu' or 'python')
        :return: Boolean indicating whether the image exists in the registry
        """
        url = f"/v2/{image_name}/tags/list"
        if self.username and self.password:
            auth = f"{self.username}:{self.password}"
            response = self._send_request('GET', url, auth)
        else:
            response = self._send_request('GET', url)

        # Check if the status code is 200
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
        return Command(['docker', 'push', image_name])

    def pull(self, image_name):
        """
        Pull an image from the Docker registry.
        
        :param image_name: Name of the image to pull (e.g., 'myrepo/myimage:tag')
        :return: Tuple of (stdout, stderr)
        """
        command = ['docker', 'pull', image_name]
        return self._run_docker_command(command)

    def login(self):
        print("> " ,['docker','login','-u',self.username,'-p',self.password])
        subprocess.run(['docker','login','-u',self.username,'-p',self.password,self.host])
