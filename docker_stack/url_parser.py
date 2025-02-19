import re
from typing import TypedDict

class ConnectionDetails(TypedDict):
    scheme: str
    host: str
    username: str
    password: str

class URLParsingError(Exception):
    """Custom exception raised when URL parsing fails."""
    pass

def is_valid_hostname(hostname: str) -> bool:
    """
    https://stackoverflow.com/a/33214423/2804342
    :return: True if for valid hostname False otherwise
    """
    if hostname[-1] == ".":
        # strip exactly one dot from the right, if present
        hostname = hostname[:-1]
    if len(hostname) > 253:
        return False

    labels = hostname.split(".")

    # the TLD must be not all-numeric
    if re.match(r"[0-9]+$", labels[-1]):
        return False

    allowed = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)$", re.IGNORECASE)
    return all(allowed.match(label) for label in labels)
def is_valid_hostport(str):
    results=str.split(':')
    def is_valid_integer(s):
        try:
            int(s)  # Try converting the string to an integer
            return True
        except ValueError:
            return False
    if len(results)==2:
        return is_valid_hostname(results[0]) and is_valid_integer(results[1])
    elif len(results)==1:
            return is_valid_hostname(results[0])
    else:
        return False
def parse_url(url)-> ConnectionDetails:
    """
    Parses a URL and returns a dictionary with scheme, host, username, and password.

    Args:
        url (str): The URL string to be parsed.

    Returns:
        dict: A dictionary containing parsed components of the URL.
            {
                'scheme': 'https' or 'http', 
                'host': The host of the URL, 
                'username': The username (if any),
                'password': The password (if any)
            }

    Raises:
        URLParsingError: If the URL cannot be parsed.
    """
    # Regular expression to match the URL with scheme, host:username:password or host:port:username:password format
    pattern = re.compile(r'^(?P<scheme>https?|ftp)://(?:(?P<username>[^:@]+)(?::(?P<password>[^@]+))?@)?(?P<host>[^:/]+)(?::(?P<port>\d+))?$')

    # Attempt to match the URL with the regular expression
    match = pattern.match(url)

    if match:
        scheme = match.group('scheme') if match.group('scheme') else 'https'  # Default to https
        host = match.group('host')
        port = match.group('port')
        username = match.group('username')
        password = match.group('password')

        # If there's a port, append it to the host
        if port:
            host = f"{host}:{port}"
        
        return {
            "scheme": scheme,
            "host": host,
            "username": username,
            "password": password
        }
    
    # Handle special cases where the URL is in host:username:password or host:port:username:password format (may include scheme)
    special_match = re.match(r'^(?P<scheme>https?|ftp)://(?P<host>[^:/]+)(?::(?P<port>\d+))?:(?P<username>[^:]+):(?P<password>.+)$', url)
    if special_match:
        scheme = special_match.group('scheme') if special_match.group('scheme') else 'https'  # Default to https
        host = special_match.group('host')
        port = special_match.group('port')
        username = special_match.group('username')
        password = special_match.group('password')

        # If there's a port, append it to the host
        if port:
            host = f"{host}:{port}"

        return {
            "scheme": scheme,
            "host": host,
            "username": username,
            "password": password
        }
        

    # Handle special case where no scheme is given, but it is in host:username:password or host:port:username:password format
    special_no_scheme_match = re.match(r'^(?P<host>[^:/]+)(?::(?P<port>\d+))?:(?P<username>[^:]+):(?P<password>.+)$', url)
    if special_no_scheme_match:
        host = special_no_scheme_match.group('host')
        port = special_no_scheme_match.group('port')
        username = special_no_scheme_match.group('username')
        password = special_no_scheme_match.group('password')

        # If there's a port, append it to the host
        if port:
            host = f"{host}:{port}"

        return {
            "scheme": 'https',  # Default to https for the special case
            "host": host,
            "username": username,
            "password": password
        }
    
    simple_regex = r'^(?:(?P<username>\S+):(?P<password>\S+)@)?(?P<hostport>\S+)(?::\d+)?$'
    match = re.match(simple_regex, url)

    if match:
        username = match.group("username")
        password = match.group("password")
        hostport = match.group("hostport")
        if is_valid_hostport(hostport):
            return {
            "scheme": "https",
            "host": hostport,
            "username": username,
            "password": password
        }        
    # If no match was found, raise a parsing error
    raise URLParsingError(f"Failed to parse URL: {url}")


# Run tests when this file is executed directly
if __name__ == "__main__":
    # Test cases
    test_urls = [
        ("registry.sireto.io", {'scheme': 'https', 'host': 'registry.sireto.io', 'username': None, 'password': None}),
        ("user:password@registry.sireto.io", {'scheme': 'https', 'host': 'registry.sireto.io', 'username': 'user', 'password': 'password'}),
        ("https://registry.sireto.io", {'scheme': 'https', 'host': 'registry.sireto.io', 'username': None, 'password': None}),
        ("https://user:password@registry.sireto.io", {'scheme': 'https', 'host': 'registry.sireto.io', 'username': 'user', 'password': 'password'}),
        ("registry.sireto.io:user:password", {'scheme': 'https', 'host': 'registry.sireto.io', 'username': 'user', 'password': 'password'}),
        ("registry.sireto.io:5050:user:password", {'scheme': 'https', 'host': 'registry.sireto.io:5050', 'username': 'user', 'password': 'password'}),
        ("http://registry.sireto.io:5050:user:password", {'scheme': 'http', 'host': 'registry.sireto.io:5050', 'username': 'user', 'password': 'password'}),
        ("https://registry.sireto.io:5050:user:password", {'scheme': 'https', 'host': 'registry.sireto.io:5050', 'username': 'user', 'password': 'password'}),
        ("registry.sireto.io:5050:user:password", {'scheme': 'https', 'host': 'registry.sireto.io:5050', 'username': 'user', 'password': 'password'}),
        ("registry.sireto.io:user:password", {'scheme': 'https', 'host': 'registry.sireto.io', 'username': 'user', 'password': 'password'})
    ]
    
    for url, expected in test_urls:
        try:
            print(f"Testing URL: {url}")
            result = parse_url(url)
            assert result == expected, f"Test failed for {url}. Expected {expected}, got {result}"
            print(f"Success: {result}")
        except Exception as e:
            print(f"Error: {e}")
