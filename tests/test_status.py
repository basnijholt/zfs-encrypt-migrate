"""Tests for the read-only file-move status report."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from zfs_encrypt_migrate import status
from zfs_encrypt_migrate.constants import MARK_ADMITTED, MARK_DIRS, MARK_KEY, MARK_SOURCE
from zfs_encrypt_migrate.filemove import _run_lock
from zfs_encrypt_migrate.status import _lock_state, file_move_status

from .conftest import STAGE, FileMoveEnv

DATASET = "tank/media"
STAGED = f"{STAGE}/media"
READ_VERBS = {"list", "get"}

pytestmark = pytest.mark.skipif(not Path("/proc/locks").exists(), reason="needs /proc/locks")


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    """Path of a lock file that does not exist yet."""
    return tmp_path / "lock"


def report(env: FileMoveEnv, lock: Path) -> str:
    """Run the status and return its text; assert that only read commands ran."""
    lines = file_move_status(env.zfs, STAGE, DATASET, lock_path=lock)
    assert env.sim.ops() == []
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


def test_not_started(filemove_env: FileMoveEnv, lock: Path) -> None:
    """Without a staged dataset nothing started; the next step is to run file-move."""
    text = report(filemove_env, lock)
    assert "phase: not started" in text
    assert f"{STAGED}: does not exist" in text
    assert "lock: no run" in text
    assert "next: run file-move" in text


def test_copying_tree(filemove_env: FileMoveEnv, lock: Path) -> None:
    """A staged dataset that is not admitted is still copying the tree."""
    stage(filemove_env, dirs=True, key="prompt")
    filemove_env.sim.prop(STAGED, "used", "2048")
    filemove_env.sim.prop(STAGED, "logicalused", "4096")
    filemove_env.sim.prop(DATASET, "used", "1048576")
    text = report(filemove_env, lock)
    assert "phase: copying the tree" in text
    assert f"{MARK_SOURCE}: {DATASET}" in text
    assert f"{MARK_KEY}: prompt" in text
    assert f"{MARK_ADMITTED}: no" in text
    assert f"{MARK_DIRS}: yes" in text
    assert f"{STAGED}: used 2.0K, logicalused 4.0K" in text
    assert f"{DATASET}: used 1.0M, logicalused 0B" in text
    assert "next: rerun the same file-move command" in text


def test_dirs_and_key_not_done(filemove_env: FileMoveEnv, lock: Path) -> None:
    """Unset markers read as no."""
    stage(filemove_env)
    text = report(filemove_env, lock)
    assert f"{MARK_DIRS}: no" in text
    assert f"{MARK_KEY}: no" in text


def test_admitted_phase_from_properties_only(filemove_env: FileMoveEnv, lock: Path) -> None:
    """Admitted is reported without counting files, whatever the source holds."""
    stage(filemove_env, admitted=True, dirs=True, key="prompt")
    text = report(filemove_env, lock)
    assert "phase: moving files or finishing (file count not checked)" in text
    assert "do not destroy either" in text


def test_status_opens_nothing_on_the_mountpoints(
    filemove_env: FileMoveEnv,
    lock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status never lists, opens or reads the source or staged mountpoint, so it cannot hold a mount busy."""
    stage(filemove_env, admitted=True, dirs=True, key="prompt")
    filemove_env.sim.prop(STAGED, "mountpoint", str(filemove_env.mount_root))
    seen: list[str] = []
    for name in ("scandir", "listdir", "open"):
        real = getattr(os, name)

        def spy(*args: object, _real: object = real, **kwargs: object) -> object:
            seen.append(str(args[0]) if args else "")
            return _real(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(os, name, spy)
    monkeypatch.setattr(Path, "iterdir", lambda self: seen.append(str(self)) or iter(()))
    monkeypatch.setattr(Path, "open", lambda self, *_a, **_k: seen.append(str(self)))
    report(filemove_env, lock)
    assert not [p for p in seen if str(filemove_env.src) in p or str(filemove_env.mount_root) in p]


def test_swapped(filemove_env: FileMoveEnv, lock: Path) -> None:
    """No staged dataset and an encrypted dataset in its place is swapped."""
    sim = filemove_env.sim
    sim.datasets |= {f"{STAGE}/old", f"{STAGE}/old/media"}
    sim.prop(DATASET, "encryption", "aes-256-gcm")
    text = report(filemove_env, lock)
    assert "phase: swapped" in text
    assert f"zfs load-key -n {DATASET}" in text
    assert f"zfs destroy -r {STAGE}/old/media" in text


def test_swapped_with_markers_left(filemove_env: FileMoveEnv, lock: Path) -> None:
    """Marker properties left on the swapped dataset mean the swap was interrupted."""
    sim = filemove_env.sim
    sim.prop(DATASET, "encryption", "aes-256-gcm")
    sim.prop(DATASET, MARK_SOURCE, DATASET)
    text = report(filemove_env, lock)
    assert "phase: swap interrupted" in text
    assert "recovery hints" in text
    assert "zfs inherit" not in text
    assert "destroy" not in text


def test_swap_stopped_after_source_marker_removed(filemove_env: FileMoveEnv, lock: Path) -> None:
    """MARK_SOURCE goes first; other markers or an unmounted dataset still mean the swap is unfinished."""
    sim = filemove_env.sim
    sim.datasets |= {f"{STAGE}/old", f"{STAGE}/old/media"}
    sim.prop(DATASET, "encryption", "aes-256-gcm")
    sim.prop(DATASET, MARK_ADMITTED, "yes")
    assert "phase: swap interrupted" in report(filemove_env, lock)
    sim.props.pop((DATASET, MARK_ADMITTED))
    sim.prop(DATASET, "mounted", "no")
    sim.prop(DATASET, "canmount", "noauto")
    text = report(filemove_env, lock)
    assert "phase: swap interrupted" in text
    assert "destroy" not in text


def test_already_encrypted_without_move_state(filemove_env: FileMoveEnv, lock: Path) -> None:
    """An encrypted dataset with no old copy and no markers has no file-move state."""
    filemove_env.sim.prop(DATASET, "encryption", "aes-256-gcm")
    text = report(filemove_env, lock)
    assert "already encrypted; no file-move state found" in text
    assert "destroy" not in text


def test_lock_stat_error_is_unknown(lock: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError on the lock file other than a missing file is reported as unknown."""

    def broken(_self: Path, **_kw: object) -> os.stat_result:
        raise PermissionError

    monkeypatch.setattr(Path, "stat", broken)
    assert _lock_state(lock).startswith("unknown")


def test_staged_of_another_dataset(filemove_env: FileMoveEnv, lock: Path) -> None:
    """A staged dataset that belongs to something else is reported, not interpreted."""
    stage(filemove_env)
    filemove_env.sim.prop(STAGED, MARK_SOURCE, "tank/other")
    text = report(filemove_env, lock)
    assert f"is not a file-move of {DATASET}" in text


def test_missing_dataset(filemove_env: FileMoveEnv, lock: Path) -> None:
    """A missing source dataset is reported with the hint about an interrupted swap."""
    filemove_env.sim.datasets.discard(DATASET)
    text = report(filemove_env, lock)
    assert f"{DATASET}: does not exist" in text
    assert "interrupted" in text


def test_lock_missing_is_no_run(lock: Path) -> None:
    """A missing lock file means no run, and the status does not create it."""
    assert _lock_state(lock) == "no run"
    assert not lock.exists()


def test_lock_unheld_file(lock: Path) -> None:
    """A lock file left by a finished run is not a running one."""
    lock.write_text("")
    assert _lock_state(lock) == "no run holds it"


def test_lock_held_and_status_does_not_disturb(lock: Path) -> None:
    """A held lock is seen; looking never takes it, and a run can start right after."""
    with _run_lock(lock):
        assert _lock_state(lock).startswith("held by a running file-move")
        assert _lock_state(lock).startswith("held by a running file-move")
    assert _lock_state(lock) == "no run holds it"
    with _run_lock(lock):
        pass


def test_lock_status_does_not_block_a_starting_run(lock: Path) -> None:
    """The status leaves no lock of its own: a run takes the lock while a status result exists."""
    lock.write_text("")
    _lock_state(lock)
    handle = lock.open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    handle.close()


def test_lock_unreadable_proc_locks(lock: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When /proc/locks cannot be read the state is unknown, never a guess."""
    lock.write_text("")

    def broken() -> str:
        raise OSError

    monkeypatch.setattr(status, "_read_locks", broken)
    assert _lock_state(lock).startswith("unknown")


def test_lock_ignores_waiters_and_other_files(lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A waiter line and a lock on another inode do not count."""
    lock.write_text("")
    other = tmp_path / "other"
    other.write_text("")
    st = lock.stat()
    dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}"
    text = (
        f"1: FLOCK  ADVISORY  WRITE 10 {dev}:{other.stat().st_ino} 0 EOF\n"
        f"2: -> FLOCK  ADVISORY  WRITE 11 {dev}:{st.st_ino} 0 EOF\n"
    )
    monkeypatch.setattr(status, "_read_locks", lambda: text)
    assert _lock_state(lock) == "no run holds it"
    monkeypatch.setattr(status, "_read_locks", lambda: f"3: FLOCK  ADVISORY  WRITE 12 {dev}:{st.st_ino} 0 EOF\n")
    assert _lock_state(lock) == "held by a running file-move (pid 12)"


def test_held_lock_says_wait(filemove_env: FileMoveEnv, lock: Path) -> None:
    """With a run active the next step is to wait."""
    stage(filemove_env, admitted=True, dirs=True, key="prompt")
    with _run_lock(lock):
        text = report(filemove_env, lock)
    assert "lock: held by a running file-move" in text
    assert "next: wait" in text
    assert "rerun" not in text


def test_status_report_is_a_list_of_text(filemove_env: FileMoveEnv, lock: Path) -> None:
    """The report is plain lines."""
    lines = file_move_status(filemove_env.zfs, STAGE, DATASET, lock_path=lock)
    assert all(isinstance(line, str) for line in lines)
