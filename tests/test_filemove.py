"""Tests for the file-move setup: preflight, staged dataset, directory tree and passphrase check."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate import filemove
from zfs_encrypt_migrate.constants import MARK_DIRS, MARK_KEY, MARK_SOURCE, SCRATCH, SPACE_ATTEMPTS, SPACE_DELAY_SECONDS
from zfs_encrypt_migrate.errors import MigrationError
from zfs_encrypt_migrate.filemove import (
    check_passphrase,
    copy_tree,
    move_files,
    open_staged,
    preflight_file_move,
    probe_writers,
    run_lock,
    wait_for_space,
)
from zfs_encrypt_migrate.migrate import AbortGuard

from .conftest import STAGE, FileMoveEnv, fail, needs_rsync
from .helpers import fail_on, make

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from zfs_encrypt_migrate.migrate import Settings
    from zfs_encrypt_migrate.process import CommandResult

DATASET = "tank/media"
STAGED = f"{STAGE}/media"


def refused(env: FileMoveEnv, settings: Settings, message: str, dataset: str = DATASET) -> None:
    """Refused."""
    with pytest.raises(MigrationError) as info:
        preflight_file_move(env.zfs, settings, dataset)
    assert info.value.message == message
    assert env.sim.ops() == []


def test_preflight_returns_source_mountpoint(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight returns source mountpoint."""
    assert preflight_file_move(filemove_env.zfs, settings, DATASET) == filemove_env.src
    assert filemove_env.sim.ops() == []


def test_preflight_refuses_bad_key_file(filemove_env: FileMoveEnv, settings: Settings, key_file: Path) -> None:
    """Preflight refuses bad key file."""
    key_file.chmod(0o644)
    refused(filemove_env, settings, f"key file {key_file} must have mode 600")


def test_preflight_refuses_pool_root(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses pool root."""
    refused(filemove_env, settings, "tank is a pool root; file-move is not supported", "tank")


def test_preflight_refuses_other_pool(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses other pool."""
    with pytest.raises(MigrationError, match="cannot rename across pools"):
        preflight_file_move(filemove_env.zfs, settings, "other/media")
    assert filemove_env.sim.ops() == []


def test_preflight_refuses_nested(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses nested."""
    with pytest.raises(MigrationError, match="overlaps the stage"):
        preflight_file_move(filemove_env.zfs, settings, f"{STAGE}/x")
    assert filemove_env.sim.ops() == []


def test_preflight_refuses_missing(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses missing."""
    refused(filemove_env, settings, "dataset tank/nope does not exist", "tank/nope")


def test_preflight_refuses_volume(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses volume."""
    filemove_env.sim.volumes.add(DATASET)
    refused(filemove_env, settings, f"{DATASET} is a volume; file-move moves the files of a filesystem")


def test_preflight_refuses_children(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses children."""
    filemove_env.sim.datasets.add("tank/media/kid")
    refused(filemove_env, settings, f"{DATASET} has child datasets; use the tree commands, or file-move each child")


def test_preflight_refuses_encrypted(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses encrypted."""
    filemove_env.sim.prop(DATASET, "encryption", "aes-256-gcm")
    refused(filemove_env, settings, f"{DATASET} is already encrypted")


def test_preflight_refuses_readonly(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses readonly."""
    filemove_env.sim.prop(DATASET, "readonly", "on")
    refused(filemove_env, settings, f"{DATASET} is read-only; file-move must delete its files")


@pytest.mark.parametrize("value", ["none", "legacy"])
def test_preflight_refuses_unmountable_mountpoint(filemove_env: FileMoveEnv, settings: Settings, value: str) -> None:
    """Preflight refuses unmountable mountpoint."""
    filemove_env.sim.prop(DATASET, "mountpoint", value)
    refused(filemove_env, settings, f"{DATASET} has mountpoint {value}; file-move needs a mounted path")


def test_preflight_refuses_unmounted(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses unmounted."""
    filemove_env.sim.prop(DATASET, "mounted", "no")
    refused(filemove_env, settings, f"{DATASET} is not mounted")


def test_preflight_refuses_reserved_name(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses reserved name."""
    reserved = filemove_env.src / SCRATCH
    reserved.mkdir()
    refused(filemove_env, settings, f"{reserved} exists; file-move needs that name for its own files")


def test_preflight_refuses_foreign_staged(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses foreign staged."""
    filemove_env.sim.datasets.add(STAGED)
    refused(filemove_env, settings, f"{STAGED} exists and is not a file-move of {DATASET}")


def test_preflight_accepts_own_staged(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight accepts own staged."""
    filemove_env.sim.datasets.add(STAGED)
    filemove_env.sim.prop(STAGED, MARK_SOURCE, DATASET)
    assert preflight_file_move(filemove_env.zfs, settings, DATASET) == filemove_env.src


def test_preflight_refuses_other_in_flight(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses other in flight."""
    filemove_env.sim.datasets.add(f"{STAGE}/other")
    refused(filemove_env, settings, f"a migration is already in flight under {STAGE}")


def test_preflight_refuses_existing_old(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Preflight refuses existing old."""
    filemove_env.sim.datasets |= {f"{STAGE}/old", f"{STAGE}/old/media"}
    refused(filemove_env, settings, f"{STAGE}/old/media already exists")


def test_run_lock_refuses_second_holder(tmp_path: Path) -> None:
    """Run lock refuses second holder."""
    lock = tmp_path / "lock"
    with run_lock(lock), pytest.raises(MigrationError) as info, run_lock(lock):
        pass
    assert info.value.message == f"another file-move is running (lock {lock})"
    with run_lock(lock):
        pass


def test_probe_writers_busy_refuses(filemove_env: FileMoveEnv) -> None:
    """Probe writers busy refuses."""
    filemove_env.sim.busy.add(DATASET)
    with pytest.raises(MigrationError) as info:
        probe_writers(filemove_env.zfs, DATASET)
    assert info.value.message == f"cannot unmount {DATASET}; a writer is probably still running"
    assert ("zfs", "mount", DATASET) not in filemove_env.sim.ops()


def test_probe_writers_remounts(filemove_env: FileMoveEnv) -> None:
    """Probe writers remounts."""
    probe_writers(filemove_env.zfs, DATASET)
    assert filemove_env.sim.ops() == [("zfs", "unmount", DATASET), ("zfs", "mount", DATASET)]


def test_probe_writers_failed_mount_hints(filemove_env: FileMoveEnv) -> None:
    """Probe writers failed mount hints."""
    zfs, _ = make(filemove_env.sim, fail_on("zfs", "mount"))
    with pytest.raises(MigrationError) as info:
        probe_writers(zfs, DATASET)
    assert f"remount it with: zfs mount {DATASET}" in info.value.hints


def guard_for(env: FileMoveEnv, settings: Settings) -> AbortGuard:
    """Guard for."""
    return AbortGuard(env.zfs, settings, env.staged, source=DATASET, leaf="media", label="file-move")


def test_open_staged_creates_with_planned_props(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Open staged creates with planned props."""
    env = filemove_env
    env.sim.prop(DATASET, "compression", "zstd")
    guard = guard_for(env, settings)
    path = open_staged(env.zfs, settings, DATASET, env.mount_root, guard)
    create = next(call for call in env.sim.log if call[1] == "create" and call[-1] == STAGED)
    options = {create[i + 1] for i, arg in enumerate(create) if arg == "-o"}
    assert {
        "encryption=aes-256-gcm",
        "canmount=noauto",
        f"mountpoint={env.mount_root}/media",
        f"{MARK_SOURCE}={DATASET}",
        "compression=zstd",
    } <= options
    assert path == env.mount_root / "media"
    assert path.is_dir()
    assert guard.mounted == [STAGED]
    assert {STAGE, f"{STAGE}/old"} <= env.sim.datasets


def test_open_staged_reuses_mounted_staged(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Open staged reuses mounted staged."""
    env = filemove_env
    open_staged(env.zfs, settings, DATASET, env.mount_root, guard_for(env, settings))
    env.sim.log.clear()
    guard = guard_for(env, settings)
    open_staged(env.zfs, settings, DATASET, env.mount_root, guard)
    assert env.sim.ops() == []
    assert guard.mounted == [STAGED]

    env.sim.prop(STAGED, "keystatus", "unavailable")
    env.sim.prop(STAGED, "mounted", "no")
    env.sim.log.clear()
    open_staged(env.zfs, settings, DATASET, env.mount_root, guard_for(env, settings))
    assert env.sim.ops() == [("zfs", "load-key", STAGED), ("zfs", "mount", STAGED)]


@needs_rsync
def test_copy_tree_copies_dirs_twice_and_marks(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Copy tree copies dirs twice and marks."""
    env = filemove_env
    guard = guard_for(env, settings)
    dst = open_staged(env.zfs, settings, DATASET, env.mount_root, guard)
    copy_tree(env.zfs, STAGED, env.src, dst)
    for root in (dst, dst / SCRATCH / "dirs"):
        for rel in ("docs", "docs/deep", "empty"):
            assert (root / rel).is_dir()
            assert (root / rel).stat().st_mtime == (env.src / rel).stat().st_mtime
        assert not (root / "a.txt").exists()
        assert not (root / "docs" / "b.txt").exists()
    assert env.sim.props[STAGED, MARK_DIRS] == "copied"


@needs_rsync
def test_copy_tree_skips_when_marked(filemove_env: FileMoveEnv) -> None:
    """Copy tree skips when marked."""
    env = filemove_env
    env.sim.prop(STAGED, MARK_DIRS, "copied")
    env.sim.datasets.add(STAGED)
    copy_tree(env.zfs, STAGED, env.src, env.mount_root)
    assert not [call for call in env.runner.calls if call[0] == "rsync"]


def staged_env(env: FileMoveEnv) -> FileMoveEnv:
    """Staged env."""
    env.sim.datasets.add(STAGED)
    return env


def test_check_passphrase_prompt_needs_terminal(
    filemove_env: FileMoveEnv,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check passphrase prompt needs terminal."""
    monkeypatch.setattr(filemove, "stdin_is_terminal", lambda: False)
    with pytest.raises(MigrationError) as info:
        check_passphrase(staged_env(filemove_env).zfs, settings, STAGED)
    assert info.value.message == "run file-move once from a terminal so it can check the passphrase"
    assert filemove_env.sim.ops() == []


def test_check_passphrase_prompt_attached(
    filemove_env: FileMoveEnv,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check passphrase prompt attached."""
    monkeypatch.setattr(filemove, "stdin_is_terminal", lambda: True)
    check_passphrase(staged_env(filemove_env).zfs, settings, STAGED)
    assert filemove_env.runner.attached == [("zfs", "load-key", "-n", "-L", "prompt", STAGED)]
    assert filemove_env.sim.props[STAGED, MARK_KEY] == "yes"


def test_check_passphrase_file_location_not_attached(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Check passphrase file location not attached."""
    settings = replace(settings, final_keylocation="file:///keys/final")
    check_passphrase(staged_env(filemove_env).zfs, settings, STAGED)
    assert filemove_env.runner.attached == []
    assert ("zfs", "load-key", "-n", "-L", "file:///keys/final", STAGED) in filemove_env.runner.calls
    assert filemove_env.sim.props[STAGED, MARK_KEY] == "yes"


def test_check_passphrase_wrong_key_refuses(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Check passphrase wrong key refuses."""
    settings = replace(settings, final_keylocation="file:///keys/final")
    zfs, _ = make(staged_env(filemove_env).sim, fail_on("zfs", "load-key", "-n"))
    with pytest.raises(MigrationError) as info:
        check_passphrase(zfs, settings, STAGED)
    assert info.value.message == f"the key for {STAGED} did not load from file:///keys/final; nothing was deleted"
    assert (STAGED, MARK_KEY) not in filemove_env.sim.props


def test_check_passphrase_skips_when_marked(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Check passphrase skips when marked."""
    staged_env(filemove_env).sim.prop(STAGED, MARK_KEY, "yes")
    check_passphrase(filemove_env.zfs, settings, STAGED)
    assert filemove_env.runner.attached == []
    assert not [call for call in filemove_env.runner.calls if "load-key" in call]


# --- move loop ---


def recorder() -> tuple[list[float], Callable[[float], None]]:
    """Return the list of sleeps and a fake sleep that records them."""
    slept: list[float] = []
    return slept, slept.append


def tamper_after_rsync(env: FileMoveEnv, action: Callable[[int], None]) -> None:
    """Run ``action(rsync_count)`` right after every real rsync."""
    inner = env.runner.responder
    count = 0

    def responder(args: Sequence[str]) -> CommandResult:
        nonlocal count
        result = inner(args)
        if args[0] == "rsync":
            count += 1
            action(count)
        return result

    env.runner.responder = responder


def move(env: FileMoveEnv, settings: Settings, dst: Path, batch_size: int, sleep: Callable[[float], None]) -> None:
    """Run the move loop on the environment."""
    move_files(env.zfs, settings, DATASET, env.src, dst, batch_size, sleep)


def source_files(env: FileMoveEnv) -> list[str]:
    """Names of the non-directory entries left in the source."""
    return sorted(str(p.relative_to(env.src)) for p in env.src.rglob("*") if not p.is_dir() or p.is_symlink())


@needs_rsync
def test_moves_everything_in_batches(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Moves everything in batches."""
    env = filemove_env
    os.link(env.src / "a.txt", env.src / "docs" / "deep" / "a-link.txt")
    (env.src / "link").symlink_to("a.txt")
    (env.src / "big.bin").write_bytes(b"x" * 20000)
    dst = tmp_path / "dst"
    dst.mkdir()
    _slept, sleep = recorder()
    move(env, settings, dst, 1, sleep)
    assert source_files(env) == []
    assert (dst / "docs" / "b.txt").read_text() == "b"
    assert (dst / "big.bin").stat().st_size == 20000  # noqa: PLR2004
    assert (dst / "link").readlink() == Path("a.txt")
    assert (dst / "a.txt").stat().st_ino == (dst / "docs" / "deep" / "a-link.txt").stat().st_ino
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("batch ")]
    assert len(lines) == 5  # noqa: PLR2004
    assert lines[0].startswith("batch 1: ")
    assert lines[-1].endswith(" left")
    assert lines[-1].startswith("batch 5: ")
    assert ("zpool", "sync", "tank") in env.sim.ops()


@needs_rsync
def test_snapshot_appearing_mid_run_stops_before_next_batch(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Snapshot appearing mid run stops before next batch."""
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    tamper_after_rsync(env, lambda n: env.sim.snapshot(f"{DATASET}@auto") if n == 1 else None)
    with pytest.raises(MigrationError) as info:
        move(env, settings, dst, 1, recorder()[1])
    assert info.value.message == f"{DATASET} has snapshots, which keep deleted files allocated: {DATASET}@auto"
    assert info.value.hints == (f"disable snapshot tools for {DATASET}, then remove them: zfs destroy {DATASET}@auto",)
    moved = sorted(str(p.relative_to(dst)) for p in dst.rglob("*.txt"))
    assert len(moved) == 1
    assert len(source_files(env)) == 2  # noqa: PLR2004
    assert not (env.src / moved[0]).exists()


def test_waits_for_freed_space(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Waits for freed space."""
    env = filemove_env
    env.sim.avail = 100
    syncs = 0
    inner = env.runner.responder

    def responder(args: Sequence[str]) -> CommandResult:
        nonlocal syncs
        if args[0] == "zpool":
            syncs += 1
            if syncs == 2:  # noqa: PLR2004
                env.sim.avail = 10**9
        return inner(args)

    env.runner.responder = responder
    slept, sleep = recorder()
    wait_for_space(env.zfs, settings, 5000, sleep)
    assert slept == [SPACE_DELAY_SECONDS, SPACE_DELAY_SECONDS]
    assert syncs == 2  # noqa: PLR2004


def test_refuses_when_space_never_frees(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Refuses when space never frees."""
    env = filemove_env
    env.sim.avail = 100
    slept, sleep = recorder()
    with pytest.raises(MigrationError) as info:
        wait_for_space(env.zfs, settings, 5000, sleep)
    assert info.value.message == ("not enough free space on tank for the next batch (4.9K) above the floor 1000B")
    reads = [c for c in env.sim.log if c[1:2] == ("list",) and "avail" in c]
    assert len(reads) == SPACE_ATTEMPTS
    assert len(slept) == SPACE_ATTEMPTS - 1


@needs_rsync
def test_rsync_failure_deletes_nothing(filemove_env: FileMoveEnv, settings: Settings, tmp_path: Path) -> None:
    """Rsync failure deletes nothing."""
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    before = source_files(env)
    inner = env.runner.responder

    def responder(args: Sequence[str]) -> CommandResult:
        return fail("boom", 23) if args[0] == "rsync" else inner(args)

    env.runner.responder = responder
    with pytest.raises(MigrationError, match="failed: boom"):
        move(env, settings, dst, 10**9, recorder()[1])
    assert source_files(env) == before


@needs_rsync
def test_copy_mismatch_deletes_nothing(filemove_env: FileMoveEnv, settings: Settings, tmp_path: Path) -> None:
    """Copy mismatch deletes nothing."""
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    before = source_files(env)
    tamper_after_rsync(env, lambda _n: (dst / "docs" / "b.txt").write_text("corrupted"))
    with pytest.raises(MigrationError, match="differs from its copy"):
        move(env, settings, dst, 10**9, recorder()[1])
    assert source_files(env) == before


@needs_rsync
def test_changing_file_is_skipped_then_refused(filemove_env: FileMoveEnv, settings: Settings, tmp_path: Path) -> None:
    """Changing file is skipped then refused."""
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    counter = iter(range(2, 1000))

    def touch(_n: int) -> None:
        (env.src / "a.txt").write_text("a" * next(counter))

    tamper_after_rsync(env, touch)
    with pytest.raises(MigrationError) as info:
        move(env, settings, dst, 10**9, recorder()[1])
    assert info.value.message == "1 files kept changing during the move; stop the writers and rerun"
    assert source_files(env) == ["a.txt"]
    assert (dst / "docs" / "b.txt").read_text() == "b"
