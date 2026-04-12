import subprocess
from typing import List, Optional


def run_command(
    command: List[str],
    stdin: Optional[str] = None,
    raise_error: bool = True,
    log: bool = True,
    shell: bool = False,
    interactive: bool = False,
    cwd=None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    if log:
        print("> " + " ".join(command), flush=True)

    result = subprocess.run(
        command,
        input=stdin,
        text=True,
        capture_output=capture_output and not interactive,
        check=False,
        shell=shell,
        cwd=cwd,
    )

    if raise_error and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )

    return result


def read_command_output(
    command: List[str],
    stdin: Optional[str] = None,
    raise_error: bool = True,
    log: bool = True,
    shell: bool = False,
    cwd=None,
) -> str:
    result = run_command(
        command,
        stdin=stdin,
        raise_error=raise_error,
        log=log,
        shell=shell,
        cwd=cwd,
        capture_output=True,
    )
    return (result.stdout or "").strip()
