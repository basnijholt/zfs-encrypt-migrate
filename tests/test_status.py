"""Tests for the read-only file-move status report."""

from __future__ import annotations

import builtins
import fcntl
import io
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate import status
from zfs_encrypt_migrate.constants import MARK_ADMITTED, MARK_DIRS, MARK_KEY, MARK_SOURCE
from zfs_encrypt_migrate.filemove import _run_lock
from zfs_encrypt_migrate.status import _lock_state, file_move_status

from .conftest import STAGE, FileMoveEnv

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from zfs_encrypt_migrate.process import CommandResult


DATASET = "tank/media"
STAGED = f"{STAGE}/media"
READ_VERBS = {"list", "get"}


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    """Path of a lock file that does not exist yet."""
    return tmp_path / "lock"


def report(env: FileMoveEnv, lock: Path) -> str:
    """Run the status and return its text; assert that only read commands ran."""
    lines = file_move_status(env.zfs, STAGE, DATASET, lock)
    assert {call[1] for call in env.sim.log} <= READ_VERBS
    assert {call[0] for call in env.sim.log} == {"zfs"}
    return "\n".join(lines)


def stage(env: FileMoveEnv, *, admitted: bool = False, dirs: bool = False, key: str | None = None) -> None:
    """Create the staged dataset with the given markers."""
    sim = env.sim
    sim.datasets.add(STAGED)
    sim.prop(STAGED, MARK_SOURCE, DATASET)
    if admitted:
        sim.prop(STAGED, MARK_ADMITTED, "yes")
    if dirs:
        sim.prop(STAGED, MARK_DIRS, "copied")
    if key:
        sim.prop(STAGED, MARK_KEY, key)


def test_source_only_prints_facts(filemove_env: FileMoveEnv, lock: Path) -> None:
    """Without a staged dataset the report says so, lists the source facts and the old copy."""
    filemove_env.sim.prop(DATASET, "used", "1048576")
    filemove_env.sim.prop(DATASET, "logicalused", "2097152")
    text = report(filemove_env, lock)
    assert f"{DATASET}: exists" in text
    assert "  encryption: off" in text
    assert "  mounted: yes" in text
    assert "  used: 1048576" in text
    assert "  logicalused: 2097152" in text
    assert f"{STAGED}: does not exist" in text
    assert f"{STAGE}/old/media: does not exist" in text
    assert "lock: not held" in text


def test_staged_facts_and_markers(filemove_env: FileMoveEnv, lock: Path) -> None:
    """The staged dataset shows its facts and every marker, unset ones as zfs prints them."""
    stage(filemove_env, admitted=True, key="prompt")
    sim = filemove_env.sim
    sim.prop(STAGED, "keystatus", "available")
    sim.prop(STAGED, "used", "2048")
    text = report(filemove_env, lock)
    assert f"{STAGED}: exists" in text
    assert "  keystatus: available" in text
    assert "  used: 2048" in text
    assert f"  {MARK_SOURCE}: {DATASET}" in text
    assert f"  {MARK_KEY}: prompt" in text
    assert f"  {MARK_ADMITTED}: yes" in text
    assert f"  {MARK_DIRS}: -" in text


def test_old_copy_exists(filemove_env: FileMoveEnv, lock: Path) -> None:
    """An old copy under the stage is reported."""
    filemove_env.sim.datasets |= {f"{STAGE}/old", f"{STAGE}/old/media"}
    assert f"{STAGE}/old/media: exists" in report(filemove_env, lock)


def test_missing_source(filemove_env: FileMoveEnv, lock: Path) -> None:
    """A missing source is a fact, not an error."""
    filemove_env.sim.datasets.discard(DATASET)
    assert f"{DATASET}: does not exist" in report(filemove_env, lock)


def test_status_only_stats_the_lock_and_touches_no_mountpoint(
    filemove_env: FileMoveEnv,
    lock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status never opens or flocks the lock file, and never lists or opens anything under the mountpoints."""
    stage(filemove_env, admitted=True, dirs=True, key="prompt")
    lock.write_text("")
    proc_locks = lock.parent / "proc-locks"
    proc_locks.write_text("")
    monkeypatch.setattr(status, "_PROC_LOCKS", proc_locks)
    seen: list[str] = []

    def spied(real: Callable[..., object]) -> Callable[..., object]:
        def spy(*args: object, **kwargs: object) -> object:
            seen.append(str(args[0]) if args else "")
            return real(*args, **kwargs)

        return spy

    for owner, name in (
        (os, "open"),
        (os, "scandir"),
        (builtins, "open"),
        (io, "open"),
        (Path, "open"),
        (fcntl, "flock"),
    ):
        monkeypatch.setattr(owner, name, spied(getattr(owner, name)))
    report(filemove_env, lock)
    # Only the fixture standing in for /proc/locks may be opened.
    assert str(proc_locks) in seen
    assert not [p for p in seen if str(lock) in p or str(filemove_env.src) in p or str(filemove_env.mount_root) in p]


def test_markers_on_dataset_after_swap(filemove_env: FileMoveEnv, lock: Path) -> None:
    """A swap stopped after the second rename leaves its markers on DATASET; they are shown."""
    sim = filemove_env.sim
    sim.datasets |= {f"{STAGE}/old", f"{STAGE}/old/media"}
    sim.prop(DATASET, "encryption", "aes-256-gcm")
    sim.prop(DATASET, MARK_ADMITTED, "yes")
    sim.prop(DATASET, MARK_SOURCE, DATASET)
    text = report(filemove_env, lock)
    assert f"{STAGED}: does not exist" in text
    assert f"{STAGE}/old/media: exists" in text
    assert f"  {MARK_ADMITTED}: yes" in text
    assert f"  {MARK_SOURCE}: {DATASET}" in text


def test_lock_stat_error_is_unknown(lock: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError on the lock file other than a missing file is reported as unknown."""

    def broken(_self: Path, **_kw: object) -> os.stat_result:
        raise PermissionError

    monkeypatch.setattr(Path, "stat", broken)
    assert _lock_state(lock).startswith("unknown")


def test_lock_missing_is_no_run(lock: Path) -> None:
    """A missing lock file means no run, and the status does not create it."""
    assert _lock_state(lock) == "not held"
    assert not lock.exists()


def test_lock_unheld_file(lock: Path) -> None:
    """A lock file left by a finished run is not a running one."""
    lock.write_text("")
    assert _lock_state(lock) == "not held"


def test_lock_held_and_status_does_not_disturb(lock: Path) -> None:
    """A held lock is seen; looking never takes it, and a run can start right after."""
    with _run_lock(lock):
        assert _lock_state(lock).startswith("held by pid")
        assert _lock_state(lock).startswith("held by pid")
    assert _lock_state(lock) == "not held"
    with _run_lock(lock):
        pass


def test_lock_unreadable_proc_locks(lock: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When /proc/locks cannot be read the state is unknown, never a guess."""
    lock.write_text("")

    monkeypatch.setattr(status, "_PROC_LOCKS", lock.parent / "missing-proc-locks")
    assert _lock_state(lock).startswith("unknown")


def test_lock_ignores_waiters_and_other_files(lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A waiter line and a lock on another inode do not count."""
    lock.write_text("")
    fake_locks = tmp_path / "proc-locks"
    monkeypatch.setattr(status, "_PROC_LOCKS", fake_locks)
    other = tmp_path / "other"
    other.write_text("")
    st = lock.stat()
    dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}"
    text = (
        f"1: FLOCK  ADVISORY  WRITE 10 {dev}:{other.stat().st_ino} 0 EOF\n"
        f"2: -> FLOCK  ADVISORY  WRITE 11 {dev}:{st.st_ino} 0 EOF\n"
    )
    fake_locks.write_text(text)
    assert _lock_state(lock) == "not held"
    fake_locks.write_text(f"3: FLOCK  ADVISORY  WRITE 12 {dev}:{st.st_ino} 0 EOF\n")
    assert _lock_state(lock) == "held by pid 12"


def test_get_many_refuses_a_missing_property(filemove_env: FileMoveEnv) -> None:
    """A row that does not come back is an error, not a silently missing fact."""
    from zfs_encrypt_migrate.errors import MigrationError  # noqa: PLC0415

    real = filemove_env.runner.responder

    def drop_used(args: Sequence[str]) -> CommandResult:
        result = real(args)
        return replace(
            result,
            stdout="".join(row for row in result.stdout.splitlines(keepends=True) if "used" not in row),
        )

    filemove_env.runner.responder = drop_used
    with pytest.raises(MigrationError, match="did not return"):
        file_move_status(filemove_env.zfs, STAGE, DATASET, filemove_env.src.parent / "lock")
