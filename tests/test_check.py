"""Tests for file-move --check: the run's first refusal, then what only the check looks for; nothing changes."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate import filemove, preflight, status
from zfs_encrypt_migrate.constants import SCRATCH
from zfs_encrypt_migrate.filemove import _run_lock
from zfs_encrypt_migrate.preflight import check_file_move

from .fakeproc import SELF_PID, FakeProc, mount_line

if TYPE_CHECKING:
    from pathlib import Path

    from zfs_encrypt_migrate.errors import MigrationError
    from zfs_encrypt_migrate.migrate import Settings

    from .conftest import FileMoveEnv

DATASET = "tank/media"
IMMUTABLE = 0x10


@pytest.fixture(autouse=True)
def as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the check as if as root; the tests themselves are not."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)


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


def flagged(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    """Make the file ``name`` immutable to the check; return the inodes it read."""
    read: list[int] = []

    def fake(fd: int) -> int:
        st = os.fstat(fd)
        read.append(st.st_ino)
        return IMMUTABLE if st.st_ino == os.lstat(name).st_ino else 0

    monkeypatch.setattr(preflight, "_inode_flags", fake)
    return read


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
    """The run's preflight stops at its first refusal; the snapshot mounts and the flags still follow."""
    env = filemove_env
    key_file.chmod(0o644)
    env.sim.prop(DATASET, "readonly", "on")
    flagged(monkeypatch, str(env.src / "a.txt"))
    src = os.path.realpath(env.src)
    extra = mount_line(200, 100, f"{src}/.zfs/snapshot/auto", f"{DATASET}@auto")
    proc = FakeProc(tmp_path / "proc", env.sim.mountinfo().decode() + extra)

    problems = check(env, settings, proc, tmp_path)

    assert [problem.message for problem in problems] == [
        f"key file {key_file} must have mode 600",
        f"snapshot {DATASET}@auto is mounted at {src}/.zfs/snapshot/auto, as seen by PID {SELF_PID}",
        (
            f"{DATASET} has files or directories with the immutable or append-only flag, which keeps file-move from "
            "deleting them or the files in them"
        ),
    ]
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
    read = flagged(monkeypatch, str(filemove_env.src / "a.txt"))
    assert [problem.message for problem in check(filemove_env, settings, proc, tmp_path)] == [
        f"{filemove_env.src} is not where {DATASET} is mounted (nothing is mounted there)",
        f"could not check flags: {DATASET} is not mounted at {filemove_env.src}",
    ]
    assert read == []


def test_a_dataset_gone_from_its_mountpoint_before_the_walk_is_reported(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A category the check could not cover is a problem, never a silent pass."""
    monkeypatch.setattr(preflight, "mounted_at", lambda _path: None)
    assert found(check(filemove_env, settings, proc, tmp_path)) == [
        (
            f"could not check flags: {DATASET} is not mounted at {filemove_env.src}",
            ("mount it there, then check again",),
        ),
    ]


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


def test_a_refusal_after_the_mount_check_leaves_the_rest_to_check(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the dataset is known to be mounted where zfs says, its files are read, whatever the refusal."""
    (filemove_env.src / SCRATCH).mkdir()
    flagged(monkeypatch, str(filemove_env.src / "a.txt"))
    assert [problem.message.split(";")[0] for problem in check(filemove_env, settings, proc, tmp_path)] == [
        f"{filemove_env.src / SCRATCH} exists",
        (
            f"{DATASET} has files or directories with the immutable or append-only flag, which keeps file-move from "
            "deleting them or the files in them"
        ),
    ]


@pytest.mark.parametrize(
    ("locks", "state"),
    [("1: FLOCK  ADVISORY  WRITE 999 {dev}:{ino} 0 EOF\n", "held by pid 999"), (None, "unknown")],
)
def test_a_run_holding_or_maybe_holding_the_lock_is_all_there_is_to_report(  # noqa: PLR0917
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locks: str | None,
    state: str,
) -> None:
    """Whether /proc/locks shows the run's lock held or cannot be read, the check reads nothing in the run's way."""
    lock = tmp_path / "lock"
    lock.touch()
    st = lock.stat()
    fake_locks = tmp_path / "locks"
    if locks is not None:
        fake_locks.write_text(locks.format(dev=f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}", ino=st.st_ino))
    monkeypatch.setattr(status, "_PROC_LOCKS", fake_locks)
    (problem,) = check(filemove_env, settings, proc, tmp_path)
    assert problem.message == "a file-move is running; check skipped"
    assert problem.hints[0].startswith(f"its lock is {state}")
    assert filemove_env.runner.calls == []


def test_the_check_takes_no_lock(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run can take its lock while the check reads: the check never locks, shared or exclusive."""
    taken: list[bool] = []

    def take_the_lock(_fd: int) -> int:
        with _run_lock(tmp_path / "run.lock"):
            taken.append(True)
        return 0

    monkeypatch.setattr(preflight, "_inode_flags", take_the_lock)
    assert check_file_move(filemove_env.zfs, settings, DATASET, proc=proc.root, lock_path=tmp_path / "run.lock") == []
    assert taken


def test_without_root_the_check_asks_for_root(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without root, other users' mounts and files stay hidden: the check says so rather than pass."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert found(check(filemove_env, settings, proc, tmp_path)) == [
        ("run --check as root, like file-move: without root it cannot see every mount and file", ()),
    ]
    assert filemove_env.runner.calls == []


def test_with_snapshots_the_run_s_snapshot_refusal_comes_without_a_scan(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's preflight scans the dataset when it has snapshots, which could update directory access times."""
    filemove_env.sim.snapshot(f"{DATASET}@auto")
    scanned: list[Path] = []
    monkeypatch.setattr(filemove, "scan", scanned.append)
    assert found(check(filemove_env, settings, proc, tmp_path)) == [
        (
            f"{DATASET} has snapshots, which keep deleted files allocated: {DATASET}@auto",
            (f"disable snapshot tools for {DATASET}, then remove them: zfs destroy {DATASET}@auto",),
        ),
    ]
    assert scanned == []
