from .envsubst import envsubst
from .compose import read_compose_file
from .configs import update_config ,check_config

"""
Functions:

1. read_compose_file(compose_file_path):
   - Reads a Docker Compose YAML file and returns its contents as a dictionary.
   
2. create_config(config_name, config_content):
   - Creates a Docker config from the given string content. Adds a label with the SHA256 hash of the config content.
   
3. check_config(config_name):
   - Checks if a Docker config with the given name already exists.
   
4. update_config(config_name, config_content):
   - Updates an existing Docker config or creates a new one if it doesn't exist. 
     If the config exists, checks the SHA256 hash and recreates the config if the hash doesn't match.
"""
