import io
import sys

import pytest

from docker_stack import conflict_prompt
from docker_stack.conflict_prompt import ENTER, EOF, OTHER, TIMEOUT, ForceDeployPrompt


class FakeTty(io.StringIO):
    def __init__(self, *, tty=True, fileno_ok=True):
        super().__init__()
        self._tty = tty
        self._fileno_ok = fileno_ok

    def isatty(self):
        return self._tty

    def fileno(self):
        if not self._fileno_ok:
            raise io.UnsupportedOperation("fileno")
        return 0


class ScriptedReader:
    """Feeds scripted results to the prompt and records the timeouts it was given."""

    def __init__(self, results):
        self.results = list(results)
        self.timeouts = []

    def wait_for_enter(self, timeout):
        self.timeouts.append(timeout)
        return self.results.pop(0) if self.results else TIMEOUT


def _prompt(results):
    stderr = io.StringIO()
    reader = ScriptedReader(results)
    return ForceDeployPrompt(stderr, reader), stderr, reader


def _freeze_clock(monkeypatch, start=100.0):
    clock = {"now": start}
    monkeypatch.setattr(conflict_prompt.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(conflict_prompt.time, "sleep", lambda secs: clock.__setitem__("now", clock["now"] + secs))
    return clock


def test_create_returns_none_without_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeTty(tty=False))
    monkeypatch.setattr(sys, "stderr", FakeTty())
    assert ForceDeployPrompt.create() is None

    monkeypatch.setattr(sys, "stdin", FakeTty())
    monkeypatch.setattr(sys, "stderr", FakeTty(tty=False))
    assert ForceDeployPrompt.create() is None

    monkeypatch.setattr(sys, "stdin", FakeTty(fileno_ok=False))
    monkeypatch.setattr(sys, "stderr", FakeTty())
    assert ForceDeployPrompt.create() is None

    monkeypatch.setattr(sys, "stdin", None)
    assert ForceDeployPrompt.create() is None


def test_create_returns_none_for_a_background_job(monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeTty())
    monkeypatch.setattr(sys, "stderr", FakeTty())
    monkeypatch.setattr(conflict_prompt.os, "name", "posix")
    monkeypatch.setattr(conflict_prompt.os, "tcgetpgrp", lambda fd: 41, raising=False)
    monkeypatch.setattr(conflict_prompt.os, "getpgrp", lambda: 42, raising=False)

    assert ForceDeployPrompt.create() is None


def test_create_builds_a_posix_prompt_in_the_foreground(monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeTty())
    monkeypatch.setattr(sys, "stderr", FakeTty())
    monkeypatch.setattr(conflict_prompt.os, "name", "posix")
    monkeypatch.setattr(conflict_prompt.os, "tcgetpgrp", lambda fd: 42, raising=False)
    monkeypatch.setattr(conflict_prompt.os, "getpgrp", lambda: 42, raising=False)

    prompt = ForceDeployPrompt.create()

    assert isinstance(prompt, ForceDeployPrompt)
    assert isinstance(prompt._reader, conflict_prompt._PosixEnterReader)


def test_create_uses_msvcrt_on_windows(monkeypatch):
    import types

    fake_msvcrt = types.SimpleNamespace(kbhit=lambda: False, getwch=lambda: "")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(sys, "stdin", FakeTty())
    monkeypatch.setattr(sys, "stderr", FakeTty())
    monkeypatch.setattr(conflict_prompt.os, "name", "nt")

    prompt = ForceDeployPrompt.create()

    assert isinstance(prompt._reader, conflict_prompt._WindowsEnterReader)


def test_double_enter_forces(monkeypatch):
    _freeze_clock(monkeypatch)
    prompt, stderr, _ = _prompt([ENTER, ENTER])

    assert prompt.wait({"operation": "deploy", "actor": "alice"}, 5) == "force"
    output = stderr.getvalue()
    assert output.count(conflict_prompt.HINT) == 1
    assert conflict_prompt.SECOND_ENTER_HINT in output


def test_single_enter_then_timeout_keeps_waiting_and_deadline(monkeypatch):
    clock = _freeze_clock(monkeypatch)
    prompt, stderr, reader = _prompt([ENTER, TIMEOUT])

    def wait_for_enter(timeout):
        reader.timeouts.append(timeout)
        result = reader.results.pop(0)
        if result == ENTER:
            clock["now"] += 2  # the person pressed Enter two seconds in
        return result

    reader.wait_for_enter = wait_for_enter

    assert prompt.wait({}, 5) is None
    assert reader.timeouts == [5, 3], "the second wait uses what is left of the original 5s window"
    assert conflict_prompt.SECOND_ENTER_HINT in stderr.getvalue()


def test_second_enter_after_the_window_restarts_the_count(monkeypatch):
    clock = _freeze_clock(monkeypatch)
    prompt, _, reader = _prompt([ENTER, ENTER, TIMEOUT])

    def wait_for_enter(timeout):
        result = reader.results.pop(0)
        if result == ENTER:
            clock["now"] += 4  # each Enter arrives 4s after the previous event: outside the 3s window
        return result

    reader.wait_for_enter = wait_for_enter

    assert prompt.wait({}, 20) is None


def test_double_enter_spanning_two_polls_still_forces(monkeypatch):
    _freeze_clock(monkeypatch)
    prompt, _, _ = _prompt([ENTER, TIMEOUT, ENTER])

    assert prompt.wait({}, 5) is None
    assert prompt.wait({}, 5) == "force"


def test_other_input_is_ignored(monkeypatch):
    _freeze_clock(monkeypatch)
    prompt, stderr, reader = _prompt([OTHER, OTHER, TIMEOUT])

    assert prompt.wait({}, 5) is None
    assert len(reader.timeouts) == 3
    assert conflict_prompt.SECOND_ENTER_HINT not in stderr.getvalue()


def test_eof_disables_the_prompt_and_sleeps_instead(monkeypatch):
    clock = _freeze_clock(monkeypatch)
    prompt, _, reader = _prompt([EOF])

    assert prompt.wait({}, 5) is None
    assert clock["now"] == 105.0, "the remaining window was slept, not spun"
    assert prompt.wait({}, 5) is None
    assert len(reader.timeouts) == 1, "no further reads after EOF"


def test_hint_is_printed_once_across_polls(monkeypatch):
    _freeze_clock(monkeypatch)
    prompt, stderr, _ = _prompt([TIMEOUT, TIMEOUT, TIMEOUT])

    for _ in range(3):
        prompt.wait({}, 5)

    assert stderr.getvalue().count(conflict_prompt.HINT) == 1


def test_posix_reader_maps_lines(monkeypatch):
    stdin = io.StringIO("\nhello\n")
    reader = conflict_prompt._PosixEnterReader(stdin, 0)
    monkeypatch.setattr(conflict_prompt.select, "select", lambda r, w, x, t: (r, [], []))

    assert reader.wait_for_enter(1) == ENTER
    assert reader.wait_for_enter(1) == OTHER
    assert reader.wait_for_enter(1) == EOF


def test_posix_reader_reports_timeout(monkeypatch):
    reader = conflict_prompt._PosixEnterReader(io.StringIO(), 0)
    monkeypatch.setattr(conflict_prompt.select, "select", lambda r, w, x, t: ([], [], []))

    assert reader.wait_for_enter(0.5) == TIMEOUT


def test_windows_reader_reads_carriage_return(monkeypatch):
    import types

    keys = ["x", "\r"]
    fake = types.SimpleNamespace(kbhit=lambda: bool(keys), getwch=lambda: keys.pop(0))
    monkeypatch.setattr(conflict_prompt.time, "sleep", lambda s: None)
    reader = conflict_prompt._WindowsEnterReader(fake)

    assert reader.wait_for_enter(1) == OTHER
    assert reader.wait_for_enter(1) == ENTER
    assert reader.wait_for_enter(0) == TIMEOUT


@pytest.mark.parametrize("stream_name", ["stdin", "stderr"])
def test_create_under_pytest_capture_is_none(stream_name):
    # Under pytest neither stream is a terminal; this guards the CLI path that
    # calls create() unconditionally.
    assert ForceDeployPrompt.create() is None
