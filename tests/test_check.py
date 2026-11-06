"""Tests for file-move --check: the run's first refusal, then what only the check looks for; nothing changes."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate import preflight
from zfs_encrypt_migrate.constants import SCRATCH
from zfs_encrypt_migrate.filemove import _run_lock
from zfs_encrypt_migrate.preflight import check_file_move

from .conftest import STAGE
from .fakeproc import CONTAINER, FakeProc, mount_line

if TYPE_CHECKING:
    from pathlib import Path

    from zfs_encrypt_migrate.errors import MigrationError
    from zfs_encrypt_migrate.migrate import Settings

    from .conftest import FileMoveEnv

DATASET = "tank/media"
IMMUTABLE = 0x10
RUNNING = ("a file-move is running; check skipped", ("check again once it has stopped",))


@pytest.fixture(autouse=True)
def as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the check as if as root; the tests themselves are not."""
    monkeypatch.setattr(preflight, "_is_root", lambda: True)


@pytest.fixture
def proc(filemove_env: FileMoveEnv, tmp_path: Path) -> FakeProc:
    """Build a fake /proc whose checking process sees the simulated mounts."""
    return FakeProc(tmp_path / "proc", filemove_env.sim.mountinfo().decode())


def check(
    env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    dataset: str = DATASET,
) -> list[MigrationError]:
    """Check the dataset with the fake /proc and a private lock file."""
    return check_file_move(env.zfs, settings, dataset, proc=proc.root, lock_path=tmp_path / "lock")


def found(problems: list[MigrationError]) -> list[tuple[str, tuple[str, ...]]]:
    """Return each problem's message with its hints."""
    return [(problem.message, problem.hints) for problem in problems]


def assert_unchanged(env: FileMoveEnv, tmp_path: Path) -> None:
    """Nothing was written, mounted, unmounted, snapshotted, keyed or copied, and no lock file was created."""
    assert env.sim.ops() == []
    assert not [call for call in env.runner.calls if call[0] == "rsync" or "load-key" in call]
    assert not (tmp_path / "lock").exists()


def held(proc: FakeProc, lock: Path, *, waiter: bool = False) -> None:
    """List ``lock`` in /proc/locks as a run's flock, or as a run waiting for it."""
    st = lock.stat()
    arrow = "-> " if waiter else ""
    (proc.root / "locks").write_text(
        f"1: {arrow}FLOCK  ADVISORY  WRITE 999 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n",
    )


def test_a_ready_dataset_has_nothing_to_report(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
) -> None:
    """With nothing in the way, the check is empty and leaves everything as it was."""
    assert check(filemove_env, settings, proc, tmp_path) == []
    assert_unchanged(filemove_env, tmp_path)


def test_the_first_refusal_comes_with_what_only_the_check_finds(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    key_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's preflight stops at its first refusal; the holders and flags still come after it."""
    env = filemove_env
    key_file.chmod(0o644)
    env.sim.prop(DATASET, "readonly", "on")
    env.sim.snapshot(f"{DATASET}@auto")
    monkeypatch.setattr(preflight, "_inode_flags", lambda path: IMMUTABLE if path.endswith("a.txt") else 0)
    snap = os.path.realpath(env.src / ".zfs" / "snapshot" / "auto")
    proc = FakeProc(tmp_path / "proc", env.sim.mountinfo().decode() + mount_line(200, 100, snap, f"{DATASET}@auto"))
    proc.add(2345, comm="indexer", ns=CONTAINER, fds=[env.src / "docs" / "b.txt"])

    problems = check(env, settings, proc, tmp_path)

    assert [problem.message for problem in problems] == [
        f"key file {key_file} must have mode 600",
        (
            f"{DATASET} has files or directories with the immutable or append-only flag, which keeps file-move from "
            "deleting them or the files in them"
        ),
        (
            f"snapshot {DATASET}@auto is mounted at {snap}, inside {DATASET}, "
            "which makes file-move's unmount fail with busy"
        ),
        f"PID 2345 (indexer) uses {DATASET} from another mount namespace: open file {env.src / 'docs' / 'b.txt'}",
    ]
    assert problems[0].hints == ()
    assert_unchanged(env, tmp_path)


def test_a_path_that_is_not_the_dataset_is_never_read(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the mountpoint zfs gives is not the dataset's mount, the check reads nothing there."""
    filemove_env.sim.prop(DATASET, "mountpoint", f"{filemove_env.src} ")
    read: list[str] = []
    monkeypatch.setattr(preflight, "_inode_flags", lambda path: read.append(path) or 0)
    proc.add(2345, ns=CONTAINER, fds=[filemove_env.src / "a.txt"])
    assert [problem.message for problem in check(filemove_env, settings, proc, tmp_path)] == [
        f"{filemove_env.src} is not where {DATASET} is mounted (nothing is mounted there)",
    ]
    assert read == []


def test_a_missing_dataset_is_the_refusal_alone(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
) -> None:
    """Without the dataset there is nothing more to look at."""
    assert found(check(filemove_env, settings, proc, tmp_path, "tank/nope")) == [
        ("dataset tank/nope does not exist", ()),
    ]


def test_a_running_file_move_is_all_there_is_to_report(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
) -> None:
    """While /proc/locks shows a run holding its lock, the check reads nothing that could keep its unmount busy."""
    lock = tmp_path / "lock"
    lock.touch()
    held(proc, lock)
    with _run_lock(lock):
        problems = check(filemove_env, settings, proc, tmp_path)
    assert found(problems) == [RUNNING]
    assert filemove_env.runner.calls == []


def test_waiters_and_other_locks_are_no_run(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
) -> None:
    """A process waiting for the lock holds nothing, and a lock on another file is not the run's."""
    lock = tmp_path / "lock"
    lock.touch()
    held(proc, lock, waiter=True)
    other = tmp_path / "other"
    other.touch()
    with (proc.root / "locks").open("a") as locks:
        st = other.stat()
        locks.write(
            f"2: FLOCK  ADVISORY  WRITE 998 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n",
        )
    assert check(filemove_env, settings, proc, tmp_path) == []


def test_the_check_takes_no_lock(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run can take its lock while the check reads: the check never locks, shared or exclusive."""
    lock = tmp_path / "lock"
    lock.touch()
    taken: list[bool] = []

    def take_the_lock(_path: str) -> int:
        with _run_lock(lock):
            taken.append(True)
        return 0

    monkeypatch.setattr(preflight, "_inode_flags", take_the_lock)
    assert check(filemove_env, settings, proc, tmp_path) == []
    assert taken


def test_a_reserved_name_is_refused_and_the_rest_still_checked(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
) -> None:
    """A refusal after the mount check leaves the dataset's own mount to read."""
    (filemove_env.src / SCRATCH).mkdir()
    proc.add(3001, comm="sh", cwd=filemove_env.src / "docs")
    assert [problem.message for problem in check(filemove_env, settings, proc, tmp_path)] == [
        f"{filemove_env.src / SCRATCH} exists; file-move needs that name for its own files",
        f"PID 3001 (sh) uses {DATASET}: working directory {filemove_env.src / 'docs'}",
    ]
    assert f"{STAGE}/media" not in filemove_env.sim.datasets


def test_without_root_the_check_asks_for_root(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without root, other users' processes and files stay hidden: the check says so rather than pass."""
    monkeypatch.setattr(preflight, "_is_root", lambda: False)
    proc.add(2345, ns=CONTAINER, fds=[filemove_env.src / "a.txt"])
    assert found(check(filemove_env, settings, proc, tmp_path)) == [
        ("run --check as root, like file-move: without root it cannot see every process and file", ()),
    ]
    assert filemove_env.runner.calls == []
