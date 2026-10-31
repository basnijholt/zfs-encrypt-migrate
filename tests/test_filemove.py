"""Tests for the file-move setup: preflight, staged dataset, directory tree and passphrase check."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate import filemove
from zfs_encrypt_migrate.constants import MARK_DIRS, MARK_KEY, MARK_SOURCE, SCRATCH
from zfs_encrypt_migrate.errors import MigrationError
from zfs_encrypt_migrate.filemove import (
    check_passphrase,
    copy_tree,
    open_staged,
    preflight_file_move,
    probe_writers,
    run_lock,
)
from zfs_encrypt_migrate.migrate import AbortGuard

from .conftest import STAGE, FileMoveEnv, needs_rsync
from .helpers import fail_on, make

if TYPE_CHECKING:
    from pathlib import Path

    from zfs_encrypt_migrate.migrate import Settings

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
