"""Shared test fixtures."""

from __future__ import annotations

import os
import shutil
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate.migrate import Settings
from zfs_encrypt_migrate.process import CommandResult, SubprocessRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from zfs_encrypt_migrate.zfs import Zfs

    from .zfs_sim import ZfsSim

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


needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


@dataclass
class FileMoveEnv:
    """A simulated tank/media mounted at ``src`` with sample files, a stage, and a private mount root."""

    sim: ZfsSim
    zfs: Zfs
    runner: FakeRunner
    src: Path
    mount_root: Path
    dataset: str = "tank/media"

    @property
    def staged(self) -> str:
        """Name of the staged dataset."""
        return f"{STAGE}/media"


@pytest.fixture
def filemove_env(tmp_path: Path) -> FileMoveEnv:
    """Build the file-move environment; rsync commands run for real, zfs commands go to the sim."""
    # Imported here: helpers and zfs_sim import this module.
    from .helpers import make  # noqa: PLC0415
    from .zfs_sim import ZfsSim  # noqa: PLC0415

    src = tmp_path / "src"
    (src / "docs" / "deep").mkdir(parents=True)
    (src / "empty").mkdir()
    (src / "a.txt").write_text("a")
    (src / "docs" / "b.txt").write_text("b")
    (src / "docs" / "deep" / "c.txt").write_text("c")
    for index, directory in enumerate((src / "docs" / "deep", src / "docs", src / "empty", src)):
        os.utime(directory, (1_000_000_000 + index, 1_000_000_000 + index))
    sim = ZfsSim(["tank", "tank/media", STAGE])
    sim.prop("tank/media", "mountpoint", str(src))
    sim.prop("tank/media", "mounted", "yes")
    real = SubprocessRunner()

    def responder(args: Sequence[str]) -> CommandResult | None:
        return real.run(args) if args[0] == "rsync" else None

    zfs, runner = make(sim, responder)
    return FileMoveEnv(sim, zfs, runner, src, tmp_path / "run")
