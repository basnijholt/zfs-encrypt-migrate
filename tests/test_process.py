"""Tests for the process runner."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate.constants import COMMAND_STARTUP_ERROR_RETURNCODE
from zfs_encrypt_migrate.process import CommandResult, SubprocessRunner

if TYPE_CHECKING:
    from pathlib import Path

_MAX_PIPELINE_SECONDS = 10


def test_run_captures_output_and_returncode() -> None:
    """A finished command reports stdout, stderr and its exit status."""
    result = SubprocessRunner().run(["sh", "-c", "echo out; echo err >&2; exit 3"])

    assert result == CommandResult(returncode=3, stdout="out\n", stderr="err\n")


def test_run_missing_binary_returns_startup_sentinel() -> None:
    """A binary that cannot start never raises."""
    result = SubprocessRunner().run(["definitely-not-a-real-binary-xyz"])

    assert result.returncode == COMMAND_STARTUP_ERROR_RETURNCODE
    assert result.stderr


def test_pipeline_feeds_first_stdout_into_second() -> None:
    """The second command reads what the first wrote."""
    first, second = SubprocessRunner().pipeline(["printf", "abc"], ["cat"])

    assert first.returncode == 0
    assert second == CommandResult(returncode=0, stdout="abc", stderr="")


def test_pipeline_reports_first_failure_without_hanging() -> None:
    """When the first command fails early the second sees EOF and the failure is reported."""
    start = time.monotonic()
    first, second = SubprocessRunner().pipeline(["false"], ["cat"])

    assert time.monotonic() - start < _MAX_PIPELINE_SECONDS
    assert first.returncode == 1
    assert second.returncode == 0


def test_pipeline_captures_stderr_of_both_commands() -> None:
    """Each side keeps its own stderr."""
    first, second = SubprocessRunner().pipeline(
        ["sh", "-c", "echo a >&2; echo data"],
        ["sh", "-c", "cat; echo b >&2; exit 2"],
    )

    assert first.stderr == "a\n"
    assert second.returncode == 2  # noqa: PLR2004
    assert second.stdout == "data\n"
    assert second.stderr == "b\n"


def test_pipeline_missing_first_binary_returns_startup_sentinel() -> None:
    """A missing first binary is reported on the first result; the second is never started."""
    first, second = SubprocessRunner().pipeline(["definitely-not-a-real-binary-xyz"], ["cat"])

    assert first.returncode == COMMAND_STARTUP_ERROR_RETURNCODE
    assert second.returncode == COMMAND_STARTUP_ERROR_RETURNCODE
    assert "not started" in second.stderr


def test_pipeline_missing_second_binary_returns_startup_sentinel() -> None:
    """A missing second binary terminates the first and reports the sentinel on the second."""
    first, second = SubprocessRunner().pipeline(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        ["definitely-not-a-real-binary-xyz"],
    )

    assert second.returncode == COMMAND_STARTUP_ERROR_RETURNCODE
    assert first.returncode != 0


def test_pipeline_terminates_both_children_on_keyboard_interrupt(tmp_path: Path) -> None:
    """Ctrl-C mid-pipeline stops both children before the interrupt propagates."""
    pid_files = (tmp_path / "first.pid", tmp_path / "second.pid")
    first = ["sh", "-c", f"echo $$ > {pid_files[0]}; exec sleep 60"]
    second = ["sh", "-c", f"echo $$ > {pid_files[1]}; exec sleep 60"]
    timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            SubprocessRunner().pipeline(first, second)
    finally:
        timer.cancel()

    for pid_file in pid_files:
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
