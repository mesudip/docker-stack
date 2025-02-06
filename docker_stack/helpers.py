import subprocess
from typing import List, Optional


def run_cli_command(
    command: List[str],
    stdin: Optional[str] = None,
    raise_error: bool = True,
    log: bool = True,
    shell: bool = False,
    interactive: bool = False,
) -> str:
    """
    Run a CLI command and return its output.

    Args:
        command: The command to run as a list of strings.
        stdin: Input to pass to the command via stdin.
        raise_error: If True, raise an exception if the command fails.
        log: If True, log the command before executing it.
        shell: If True, run the command in a shell.
        interactive: If True, run the command with everything sent to the current console.

    Returns:
        The stdout of the command as a string, or None if interactive mode is enabled.
    """
    if log:
        print("> " + " ".join(command))

    try:
        if interactive:
            result = subprocess.run(command, shell=shell)
            return None
        else:
            result = subprocess.run(
                command,
                input=stdin,
                text=True,
                capture_output=True,
                check=raise_error,
                shell=shell,
            )
            return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        # Log the error and re-raise the exception
        print(f"Error running command: {e}")
        print(f"stdout: {e.stdout}")
        print(f"stderr: {e.stderr}")
        raise e


class Command:
    nop: "Command" = None

    def isNop(self):
        return self == Command.nop

    def __init__(
        self, command: List[str], stdin: Optional[str] = None, log: bool = True, id=None
    ):
        """
        Initialize a Command object.

        Args:
            command: The command to run as a list of strings.
            stdin: Input to pass to the command via stdin.
            log: If True, log the command before executing it.
        """
        self.command = command
        self.stdin = stdin
        self.log = log
        self.id = id

    def execute(self, log: Optional[bool] = None) -> str:
        """
        Execute the command and return its output.

        Args:
            log: If provided, overrides the log setting from the constructor.

        Returns:
            The stdout of the command as a string.
        """
        if not self.command:
            return
        # Use the provided log value if available, otherwise use the one from the constructor
        use_log = log if log is not None else self.log
        if not self.stdin:
            subprocess.run(self.command)
        return run_cli_command(self.command, stdin=self.stdin, log=use_log, shell=False)

    def __str__(self) -> str:
        """
        Return a string representation of the command.

        Returns:
            A string representation of the command, including stdin if applicable.
        """
        if self.stdin:
            return f"echo '{self.stdin}' | {' '.join(self.command)}"
        elif self.command:
            return " ".join(self.command)
        else:
            return "NOP"


Command.nop = Command([])
