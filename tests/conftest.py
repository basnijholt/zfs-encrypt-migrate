"""Shared test fixtures."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate.migrate import Settings
from zfs_encrypt_migrate.process import CommandResult

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

STAGE = "tank/stage"
OK = CommandResult(returncode=0, stdout="", stderr="")


def ok(stdout: str = "") -> CommandResult:
    """Build a successful result."""
    return CommandResult(returncode=0, stdout=stdout, stderr="")


def fail(stderr: str = "", returncode: int = 1) -> CommandResult:
    """Build a failed result."""
    return CommandResult(returncode=returncode, stdout="", stderr=stderr)


class FakeRunner:
    """Record commands and answer them from a queue, then from a responder."""

    def __init__(
        self,
        responder: Callable[[Sequence[str]], CommandResult] | None = None,
        queue: Sequence[CommandResult] = (),
    ) -> None:
        """Set the fallback responder and the queued results answered first."""
        self.responder = responder or (lambda _args: OK)
        self.queue: deque[CommandResult] = deque(queue)
        self.calls: list[tuple[str, ...]] = []
        self.attached: list[tuple[str, ...]] = []
        self.pipelines: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def _answer(self, args: Sequence[str]) -> CommandResult:
        return self.queue.popleft() if self.queue else self.responder(args)

    def run(self, args: Sequence[str]) -> CommandResult:
        """Record the command and return the next answer."""
        self.calls.append(tuple(args))
        return self._answer(args)

    def run_attached(self, args: Sequence[str]) -> int:
        """Record the command and return the exit code of the next answer."""
        self.attached.append(tuple(args))
        return self._answer(args).returncode

    def pipeline(self, first: Sequence[str], second: Sequence[str]) -> tuple[CommandResult, CommandResult]:
        """Record both commands and answer each in order."""
        self.pipelines.append((tuple(first), tuple(second)))
        return self._answer(first), self._answer(second)


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """Create a private key file."""
    path = tmp_path / "key"
    path.write_text("secret")
    path.chmod(0o600)
    return path


@pytest.fixture
def settings(key_file: Path) -> Settings:
    """Build settings with a tiny free-space floor."""
    return Settings(stage=STAGE, key_file=key_file, min_free=1000, keyformat="passphrase", final_keylocation="prompt")
