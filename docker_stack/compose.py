import yaml
import os

def read_compose_file(compose_file_path):
    """
    Reads a Docker Compose YAML file and returns its contents as a dictionary.

    :param compose_file_path: Path to the Docker Compose file.
    :return: Dictionary representation of the YAML contents.
    """
    if not os.path.exists(compose_file_path):
        raise FileNotFoundError(f"Compose file {compose_file_path} does not exist.")

    with open(compose_file_path, "r") as file:
        compose_data = yaml.safe_load(file)

    return compose_data
