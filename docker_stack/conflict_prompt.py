"""Terminal prompt shown while a deploy waits for another run of the same stack.

Docker-Manager refuses to queue interactive applies: it answers ``409
deployment_in_progress`` and leaves the choice between waiting and aborting to
whoever is on the other end. On a CLI that is the person at the terminal, so
while the client polls for the stack to free up this prompt listens for two
Enter presses in quick succession and reports ``"force"``; the caller then asks
the manager to abort the running deployment. Ctrl+C is left alone and keeps
exiting the CLI.

Enter is line-oriented, so no terminal mode is changed and there is nothing to
restore on the way out. The prompt is only created when both stdin and stderr
are terminals and the process is in the foreground; anywhere else (CI, pipes,
the GitHub Action's heredoc) the caller simply sleeps between polls.
"""

import io
import os
import select
import sys
import time
from typing import Any, Optional

DOUBLE_ENTER_SECS = 3.0
WINDOWS_POLL_SECS = 0.1
HINT = "[manager] press Enter twice to force your deploy (aborts that run; changes it already made to the daemon stay), Ctrl+C to quit"
SECOND_ENTER_HINT = f"[manager] press Enter again within {int(DOUBLE_ENTER_SECS)}s to force the deploy"

ENTER = "enter"
OTHER = "other"
TIMEOUT = "timeout"
EOF = "eof"


class _PosixEnterReader:
    def __init__(self, stdin, fd: int):
        self._stdin = stdin
        self._fd = fd

    def wait_for_enter(self, timeout: float) -> str:
        try:
            readable, _, _ = select.select([self._fd], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            return EOF
        if not readable:
            return TIMEOUT
        line = self._stdin.readline()
        if line == "":
            return EOF
        return ENTER if line.strip() == "" else OTHER


class _WindowsEnterReader:
    def __init__(self, msvcrt_module):
        self._msvcrt = msvcrt_module

    def wait_for_enter(self, timeout: float) -> str:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._msvcrt.kbhit():
                char = self._msvcrt.getwch()
                return ENTER if char in ("\r", "\n") else OTHER
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return TIMEOUT
            time.sleep(min(WINDOWS_POLL_SECS, remaining))


class ForceDeployPrompt:
    """Listens for a double Enter between polls; see the module docstring."""

    def __init__(self, stderr, reader):
        self._stderr = stderr
        self._reader = reader
        self._hinted = False
        self._dead = False
        self._first_enter_at: Optional[float] = None

    @classmethod
    def create(cls) -> Optional["ForceDeployPrompt"]:
        """Build a prompt when a person can see and answer it, else ``None``."""
        stdin, stderr = sys.stdin, sys.stderr
        if stdin is None or stderr is None:
            return None
        try:
            if not stdin.isatty() or not stderr.isatty():
                return None
            fd = stdin.fileno()
        except (AttributeError, ValueError, OSError, io.UnsupportedOperation):
            return None
        if os.name == "nt":
            try:
                import msvcrt  # type: ignore[import-not-found]
            except ImportError:
                return None
            return cls(stderr, _WindowsEnterReader(msvcrt))
        try:
            # Reading a terminal from a background job stops the process (SIGTTIN).
            if os.tcgetpgrp(fd) != os.getpgrp():
                return None
        except (AttributeError, OSError):
            return None
        return cls(stderr, _PosixEnterReader(stdin, fd))

    def _say(self, message: str) -> None:
        print(message, file=self._stderr, flush=True)

    def wait(self, active: Any, seconds: float) -> Optional[str]:
        """Wait up to ``seconds`` for the next poll; ``"force"`` if Enter was pressed twice."""
        deadline = time.monotonic() + max(0.0, seconds)
        if not self._hinted:
            self._say(HINT)
            self._hinted = True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self._dead:
                time.sleep(remaining)
                return None
            result = self._reader.wait_for_enter(remaining)
            if result == TIMEOUT:
                return None
            if result == EOF:
                self._dead = True
                continue
            if result != ENTER:
                continue
            now = time.monotonic()
            if self._first_enter_at is not None and now - self._first_enter_at <= DOUBLE_ENTER_SECS:
                self._first_enter_at = None
                return "force"
            self._first_enter_at = now
            self._say(SECOND_ENTER_HINT)
