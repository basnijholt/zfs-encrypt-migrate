"""Process runner abstractions."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Protocol

from .constants import COMMAND_STARTUP_ERROR_RETURNCODE

if TYPE_CHECKING:
    from collections.abc import Sequence

# Bound the wait for a terminated child so a process stuck in an uninterruptible
# kernel sleep cannot hang the cleanup path.
_TERMINATE_WAIT_SECONDS = 5.0


def _child_env() -> dict[str, str]:
    # Callers match English zfs messages ("does not exist", "busy"), so never
    # let the operator's locale translate them.
    return {**os.environ, "LC_ALL": "C"}


@dataclass(frozen=True)
class CommandResult:
    """Result from a local command runner."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Synchronous command runner protocol."""

    def run(self, args: Sequence[str]) -> CommandResult:
        """Run a command and return its result."""

    def pipeline(self, first: Sequence[str], second: Sequence[str]) -> tuple[CommandResult, CommandResult]:
        """Run ``first | second`` and return both results."""


def _startup_failure(exc: OSError) -> CommandResult:
    return CommandResult(returncode=COMMAND_STARTUP_ERROR_RETURNCODE, stdout="", stderr=f"{exc}\n")


def _read(handle: IO[bytes]) -> str:
    handle.seek(0)
    return handle.read().decode(errors="replace")


def _terminate(*processes: subprocess.Popen[bytes]) -> None:
    """Stop children that are still running and reap them."""
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=_TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class SubprocessRunner:
    """Run commands through subprocess."""

    def run(self, args: Sequence[str]) -> CommandResult:
        """Run a command and capture stdout/stderr."""
        # UTF-8 with replacement: a root tool must degrade gracefully on
        # non-UTF-8 child output, never traceback.
        try:
            result = subprocess.run(
                list(args),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                env=_child_env(),
            )
        except OSError as exc:
            return _startup_failure(exc)
        return CommandResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def pipeline(self, first: Sequence[str], second: Sequence[str]) -> tuple[CommandResult, CommandResult]:
        """Run ``first | second``, capturing stderr of both and stdout of the second."""
        # Output goes to temporary files rather than pipes so waiting on a
        # long-running send/recv can never deadlock on a full stderr pipe.
        with tempfile.TemporaryFile() as err1, tempfile.TemporaryFile() as err2, tempfile.TemporaryFile() as out2:
            try:
                producer = subprocess.Popen(
                    list(first),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=err1,
                    env=_child_env(),
                )
            except OSError as exc:
                return _startup_failure(exc), CommandResult(
                    returncode=COMMAND_STARTUP_ERROR_RETURNCODE,
                    stdout="",
                    stderr="not started: the first command of the pipeline failed to start\n",
                )
            try:
                try:
                    consumer = subprocess.Popen(
                        list(second),
                        stdin=producer.stdout,
                        stdout=out2,
                        stderr=err2,
                        env=_child_env(),
                    )
                except OSError as exc:
                    _terminate(producer)
                    return (
                        CommandResult(producer.returncode, "", _read(err1)),
                        _startup_failure(exc),
                    )
                finally:
                    # Close the parent's copy so the consumer sees EOF as soon
                    # as the producer exits, even when the producer fails early.
                    if producer.stdout is not None:
                        producer.stdout.close()
                try:
                    consumer.wait()
                    producer.wait()
                except BaseException:
                    # Includes KeyboardInterrupt: never leave a send or recv running.
                    _terminate(producer, consumer)
                    raise
            except BaseException:
                with contextlib.suppress(OSError):
                    _terminate(producer)
                raise
            return (
                CommandResult(producer.returncode, "", _read(err1)),
                CommandResult(consumer.returncode, _read(out2), _read(err2)),
            )
