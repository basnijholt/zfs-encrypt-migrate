"""Tests for file-move: preflight, staged dataset, directory tree, passphrase check, move loop and swap."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate import filemove, filetree
from zfs_encrypt_migrate.constants import (
    MARK_ADMITTED,
    MARK_DIRS,
    MARK_KEY,
    MARK_SOURCE,
    SCRATCH,
    SPACE_ATTEMPTS,
    SPACE_DELAY_SECONDS,
)
from zfs_encrypt_migrate.errors import Interrupted, MigrationError, ZfsError
from zfs_encrypt_migrate.filemove import (
    admit,
    check_passphrase,
    copy_tree,
    file_move,
    finish,
    move_files,
    open_staged,
    preflight_file_move,
    probe_writers,
    rsync_dirs,
    rsync_files,
    run_lock,
    wait_for_space,
)
from zfs_encrypt_migrate.migrate import AbortGuard
from zfs_encrypt_migrate.process import SubprocessRunner
from zfs_encrypt_migrate.sizes import format_size

from .conftest import STAGE, FileMoveEnv, fail, needs_rsync
from .helpers import fail_on, make
from .zfs_sim import ZfsSim

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from zfs_encrypt_migrate.filetree import Group
    from zfs_encrypt_migrate.migrate import Settings
    from zfs_encrypt_migrate.process import CommandResult
    from zfs_encrypt_migrate.zfs import Zfs

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


def test_preflight_refuses_reserved_name_as_dangling_symlink(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """A symlink by that name counts even when its target is missing: its copy would collide with scratch."""
    reserved = filemove_env.src / SCRATCH
    reserved.symlink_to("nowhere")
    refused(filemove_env, settings, f"{reserved} exists; file-move needs that name for its own files")


@pytest.mark.parametrize("prop", ["reservation", "refreservation"])
def test_preflight_refuses_reserved_space(filemove_env: FileMoveEnv, settings: Settings, prop: str) -> None:
    """A reservation keeps the space of deleted files reserved for the source, so no batch would ever fit."""
    filemove_env.sim.prop(DATASET, prop, "1073741824")
    refused(
        filemove_env,
        settings,
        f"{DATASET} has {prop}=1073741824, which keeps deleted space reserved; clear it with: "
        f"zfs set {prop}=none {DATASET} (set it again on the new dataset after the move)",
    )


def nested_source(env: FileMoveEnv) -> str:
    """Make the source tank/srv/media, under a parent tank/srv, and return its name."""
    dataset = "tank/srv/media"
    env.sim.datasets |= {"tank/srv", dataset}
    env.sim.prop(dataset, "mountpoint", str(env.src))
    env.sim.prop(dataset, "mounted", "yes")
    return dataset


def test_preflight_refuses_ancestor_reservation(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """A parent's reservation keeps the space freed in the source reserved for the parent, away from the stage."""
    dataset = nested_source(filemove_env)
    filemove_env.sim.prop("tank/srv", "reservation", "1073741824")
    refused(
        filemove_env,
        settings,
        f"tank/srv has reservation=1073741824, which keeps space freed in {dataset} reserved; clear it with: "
        "zfs set reservation=none tank/srv (set it again after the move)",
        dataset,
    )


def test_preflight_allows_ancestor_reservation_around_the_stage(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """A parent reservation that holds the stage too is spent on the copies; a refreservation covers its own data."""
    dataset = nested_source(filemove_env)
    filemove_env.sim.prop("tank/srv", "reservation", "1073741824")
    inside = replace(settings, stage="tank/srv/stage")
    assert preflight_file_move(filemove_env.zfs, inside, dataset) == filemove_env.src
    filemove_env.sim.prop("tank/srv", "reservation", "0")
    filemove_env.sim.prop("tank/srv", "refreservation", "1073741824")
    assert preflight_file_move(filemove_env.zfs, settings, dataset) == filemove_env.src


def test_preflight_refuses_clone(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """A clone's origin snapshot keeps the blocks of deleted files allocated."""
    filemove_env.sim.prop(DATASET, "origin", "tank/old@base")
    refused(
        filemove_env,
        settings,
        f"{DATASET} is a clone of tank/old@base; its origin keeps the space of deleted files; "
        f"promote it first: zfs promote {DATASET}",
    )


def test_preflight_refuses_pool_checkpoint(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """A pool checkpoint keeps every deleted block allocated until it is discarded."""
    filemove_env.sim.pool_props["tank", "checkpoint"] = "1052672"
    refused(
        filemove_env,
        settings,
        "pool tank has a checkpoint, which keeps deleted space allocated; discard it first: zpool checkpoint -d tank",
    )


def test_preflight_refuses_snapshots_before_any_write(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """A snapshot is refused before the writer probe or the staged dataset changes anything."""
    filemove_env.sim.snapshot(f"{DATASET}@auto")
    with pytest.raises(MigrationError) as info:
        preflight_file_move(filemove_env.zfs, settings, DATASET)
    assert info.value.message == SNAPSHOT_REFUSAL
    assert info.value.hints == SNAPSHOT_HINTS
    assert filemove_env.sim.ops() == []


def test_preflight_allows_snapshots_once_only_directories_are_left(
    filemove_env: FileMoveEnv,
    settings: Settings,
) -> None:
    """With no file left to delete, a snapshot pins nothing: a rerun after a refused swap keeps a renamed enc-final."""
    for name in ("a.txt", "docs/b.txt", "docs/deep/c.txt"):
        (filemove_env.src / name).unlink()
    filemove_env.sim.snapshot(f"{DATASET}@enc-final-1")
    assert preflight_file_move(filemove_env.zfs, settings, DATASET) == filemove_env.src


@pytest.mark.parametrize("value", ["on", "sha256,verify"])
def test_preflight_refuses_dedup(filemove_env: FileMoveEnv, settings: Settings, value: str) -> None:
    """Deleting a deduplicated file frees nothing while another reference to its blocks remains."""
    filemove_env.sim.prop(DATASET, "dedup", value)
    refused(
        filemove_env,
        settings,
        f"{DATASET} has dedup={value}; deleting deduplicated files frees no space, so file-move cannot run; "
        "see the docs",
    )


def test_preflight_refuses_pool_with_a_dedup_table(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Blocks deduplicated across the pool may be shared with the source, which turned dedup off since.

    dedupratio rounds a little deduplication down to 1.00x; a dedup table of any size shows it.
    """
    filemove_env.sim.pool_props["tank", "dedup_table_size"] = "4096"
    refused(
        filemove_env,
        settings,
        f"pool tank has a dedup table (4.0K); deduplicated blocks shared with {DATASET} would not be freed, "
        "so file-move cannot run; see the docs",
    )


def no_dedup_table_size(args: Sequence[str]) -> CommandResult | None:
    """Answer zpool get dedup_table_size as an OpenZFS before 2.3 does."""
    if tuple(args[:2]) == ("zpool", "get") and "dedup_table_size" in args:
        return fail("bad property list: invalid property 'dedup_table_size'\n", 2)
    return None


@pytest.mark.parametrize(("ratio", "refuses"), [("1.50", True), ("1.00", False)])
def test_preflight_falls_back_to_dedupratio(
    filemove_env: FileMoveEnv,
    settings: Settings,
    ratio: str,
    refuses: bool,  # noqa: FBT001
) -> None:
    """Without dedup_table_size (OpenZFS before 2.3), a dedupratio above 1.00x is the sign of deduplication."""
    env = filemove_env
    env.sim.pool_props["tank", "dedupratio"] = ratio
    zfs, _ = make(env.sim, no_dedup_table_size)
    if not refuses:
        assert preflight_file_move(zfs, settings, DATASET) == env.src
        return
    with pytest.raises(MigrationError) as info:
        preflight_file_move(zfs, settings, DATASET)
    assert info.value.message == (
        f"pool tank has dedupratio 1.50x; deduplicated blocks shared with {DATASET} would not be freed, "
        "so file-move cannot run; see the docs"
    )
    assert env.sim.ops() == []


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
    assert [op for op in filemove_env.sim.ops() if op[1] == "mount"] == []


def test_probe_writers_remounts_without_atime(filemove_env: FileMoveEnv) -> None:
    """The remount skips atime updates, so reading the source after enc-final never writes to it."""
    probe_writers(filemove_env.zfs, DATASET)
    assert filemove_env.sim.ops() == [("zfs", "unmount", DATASET), ("zfs", "mount", "-o", "noatime", DATASET)]


REMOUNT_HINT = f"remount it with: zfs mount {DATASET}"


def test_probe_writers_failed_mount_hints(filemove_env: FileMoveEnv) -> None:
    """A failed remount keeps its zfs error and says how to remount."""
    zfs, _ = make(filemove_env.sim, fail_on("zfs", "mount"))
    with pytest.raises(ZfsError) as info:
        probe_writers(zfs, DATASET)
    assert info.value.message == f"zfs mount -o noatime {DATASET} failed: injected"
    assert info.value.hints == (REMOUNT_HINT,)


def test_probe_writers_interrupt_before_remount_hints(filemove_env: FileMoveEnv) -> None:
    """Ctrl-C between the unmount and the remount stays an interrupt and says how to remount."""

    def interrupt_mount(args: Sequence[str]) -> CommandResult | None:
        if tuple(args[:2]) == ("zfs", "mount"):
            raise KeyboardInterrupt
        return None

    zfs, _ = make(filemove_env.sim, interrupt_mount)
    with pytest.raises(Interrupted) as info:
        probe_writers(zfs, DATASET)
    assert info.value.message == "interrupted"
    assert info.value.hints == (REMOUNT_HINT,)


def test_probe_writers_unexpected_error_before_remount_hints(filemove_env: FileMoveEnv) -> None:
    """Any other failure between the unmount and the remount also says how to remount."""

    def broken_read(args: Sequence[str]) -> CommandResult | None:
        if tuple(args[:2]) == ("zfs", "get"):
            msg = "boom"
            raise OSError(msg)
        return None

    zfs, _ = make(filemove_env.sim, broken_read)
    with pytest.raises(MigrationError) as info:
        probe_writers(zfs, DATASET)
    assert info.value.message == "OSError: boom"
    assert info.value.hints == (REMOUNT_HINT,)


def guard_for(env: FileMoveEnv, settings: Settings) -> AbortGuard:
    """Guard for."""
    return AbortGuard(env.zfs, settings, env.staged, source=DATASET, leaf="media", label="file-move")


def test_open_staged_creates_with_planned_props(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Open staged creates with planned props."""
    env = filemove_env
    env.sim.prop(DATASET, "compression", "zstd")
    guard = guard_for(env, settings)
    path, device = open_staged(env.zfs, settings, DATASET, env.zfs.props(DATASET), env.mount_root, guard)
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
    assert device == path.stat().st_dev
    assert guard.mounted == [STAGED]
    assert {STAGE, f"{STAGE}/old"} <= env.sim.datasets


def test_open_staged_refuses_a_mountpoint_nothing_is_mounted_on(
    filemove_env: FileMoveEnv,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When zfs reports the staged dataset mounted but its mountpoint is a bare directory, nothing is copied there."""
    env = filemove_env
    monkeypatch.setattr(filemove, "mount_device", lambda _path: None)
    with pytest.raises(MigrationError) as info:
        open_staged(env.zfs, settings, DATASET, env.zfs.props(DATASET), env.mount_root, guard_for(env, settings))
    assert info.value.message == f"{STAGED} is not mounted at {env.mount_root / 'media'}"


@needs_rsync
def test_staged_on_another_device_stops_the_next_batch(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Something else mounted over the staged mountpoint, while zfs still says it is mounted, stops the run too."""
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    real_delete = filetree.delete

    def delete_then_cover(src: Path, groups: Sequence[Group]) -> int:
        count = real_delete(src, groups)
        monkeypatch.setattr(filemove, "mount_device", lambda _path: dst.stat().st_dev + 1)
        return count

    monkeypatch.setattr(filemove, "delete", delete_then_cover)
    with pytest.raises(MigrationError) as info:
        move(env, settings, dst, 1, recorder()[1])
    assert info.value.message == f"{STAGED} is no longer mounted at {dst}; nothing of this batch was deleted"
    assert len(source_files(env)) == 2  # noqa: PLR2004


def test_open_staged_reuses_mounted_staged(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Open staged reuses mounted staged."""
    env = filemove_env
    open_staged(env.zfs, settings, DATASET, env.zfs.props(DATASET), env.mount_root, guard_for(env, settings))
    env.sim.log.clear()
    guard = guard_for(env, settings)
    open_staged(env.zfs, settings, DATASET, env.zfs.props(DATASET), env.mount_root, guard)
    assert env.sim.ops() == []
    assert guard.mounted == [STAGED]

    env.sim.prop(STAGED, "keystatus", "unavailable")
    env.sim.prop(STAGED, "mounted", "no")
    env.sim.log.clear()
    open_staged(env.zfs, settings, DATASET, env.zfs.props(DATASET), env.mount_root, guard_for(env, settings))
    assert env.sim.ops() == [("zfs", "load-key", STAGED), ("zfs", "mount", STAGED)]


def test_interrupt_right_after_staged_mount_unmounts_it(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Ctrl-C just as the staged mount returns still unmounts it before the key is unloaded."""
    env = filemove_env

    def interrupt_after_mount(args: Sequence[str]) -> None:
        if tuple(args) == ("zfs", "mount", STAGED):
            raise KeyboardInterrupt

    zfs, _ = make(env.sim, after=interrupt_after_mount)
    guard = AbortGuard(zfs, settings, STAGED, source=None, leaf=None, label="file-move")
    with pytest.raises(Interrupted), guard:
        open_staged(zfs, settings, DATASET, zfs.props(DATASET), env.mount_root, guard)
    assert env.sim.ops()[-2:] == [("zfs", "unmount", STAGED), ("zfs", "unload-key", "-r", STAGED)]
    assert env.sim.props[STAGED, "mounted"] == "no"


def test_failed_staged_mount_still_unloads_the_key(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """A staged dataset whose mount failed is not unmounted: zfs calls that an error, which kept the key loaded."""
    env = filemove_env

    def mount_fails(args: Sequence[str]) -> CommandResult | None:
        if tuple(args) == ("zfs", "mount", STAGED):
            return fail("injected\n")
        if tuple(args) == ("zfs", "unmount", STAGED) and env.sim.props.get((STAGED, "mounted")) != "yes":
            return fail(f"cannot unmount '{STAGED}': not currently mounted\n")
        return None

    zfs, runner = make(env.sim, mount_fails)
    guard = AbortGuard(zfs, settings, STAGED, source=None, leaf=None, label="file-move")
    with pytest.raises(ZfsError) as info, guard:
        open_staged(zfs, settings, DATASET, zfs.props(DATASET), env.mount_root, guard)
    assert info.value.hints == ()
    assert ("zfs", "unmount", STAGED) not in runner.calls
    assert env.sim.props[STAGED, "keystatus"] == "unavailable"


@needs_rsync
def test_copy_tree_copies_dirs_twice_and_marks(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Copy tree copies dirs twice and marks."""
    env = filemove_env
    guard = guard_for(env, settings)
    dst, device = open_staged(env.zfs, settings, DATASET, env.zfs.props(DATASET), env.mount_root, guard)
    copy_tree(env.zfs, STAGED, env.src, dst, device)
    for root in (dst, dst / SCRATCH / "dirs"):
        for rel in ("docs", "docs/deep", "empty"):
            assert (root / rel).is_dir()
            assert (root / rel).stat().st_mtime == (env.src / rel).stat().st_mtime
        assert not (root / "a.txt").exists()
        assert not (root / "docs" / "b.txt").exists()
    assert env.sim.props[STAGED, MARK_DIRS] == "copied"


@needs_rsync
def test_copy_tree_unmarked_when_staged_unmounted_meanwhile(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """The marker says the tree is in the staged dataset, so it is set only while that is still mounted.

    Otherwise the copies went to the bare directory left behind, and a rerun would skip copying the tree.
    """
    env = filemove_env
    guard = guard_for(env, settings)
    dst, device = open_staged(env.zfs, settings, DATASET, env.zfs.props(DATASET), env.mount_root, guard)
    tamper_after_rsync(env, lambda n: env.zfs.unmount(STAGED) if n == 2 else None)  # noqa: PLR2004
    with pytest.raises(MigrationError) as info:
        copy_tree(env.zfs, STAGED, env.src, dst, device)
    assert info.value.message == f"{STAGED} is no longer mounted at {dst}; nothing of this batch was deleted"
    assert (STAGED, MARK_DIRS) not in env.sim.props


@needs_rsync
def test_copy_tree_skips_when_marked(filemove_env: FileMoveEnv) -> None:
    """Copy tree skips when marked."""
    env = filemove_env
    env.sim.prop(STAGED, MARK_DIRS, "copied")
    env.sim.datasets.add(STAGED)
    copy_tree(env.zfs, STAGED, env.src, env.mount_root, 0)
    assert not [call for call in env.runner.calls if call[0] == "rsync"]


def test_rsync_argument_lists(tmp_path: Path) -> None:
    """The exact rsync calls: never --delete or --remove-source-files, and times compared to the nanosecond."""
    zfs, runner = make(ZfsSim())
    src, dst = tmp_path / "src", tmp_path / "dst"
    rsync_files(zfs, src, dst, dst / "list")
    rsync_dirs(zfs, src, dst)
    assert runner.calls == [
        (
            "rsync",
            "-aHAXS",
            "--numeric-ids",
            "--modify-window=-1",
            "--from0",
            f"--files-from={dst / 'list'}",
            f"--temp-dir={dst / SCRATCH / 'tmp'}",
            f"{src}/",
            f"{dst}/",
        ),
        (
            "rsync",
            "-aAX",
            "--numeric-ids",
            "--modify-window=-1",
            "--exclude=/.zfs",
            "--include=*/",
            "--exclude=*",
            f"{src}/",
            f"{dst}/",
        ),
    ]


# Two times in the same second: rsync's default --modify-window=0 compares whole seconds only.
EARLY_NS = 1_200_000_000_100_000_000
LATE_NS = 1_200_000_000_900_000_000


@needs_rsync
def test_directory_time_off_below_a_second_is_restored(filemove_env: FileMoveEnv, tmp_path: Path) -> None:
    """A directory whose copy differs from the source only in nanoseconds gets the source's time."""
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    rsync_dirs(env.zfs, env.src, dst)
    os.utime(env.src / "docs", ns=(EARLY_NS, EARLY_NS))
    os.utime(dst / "docs", ns=(LATE_NS, LATE_NS))
    rsync_dirs(env.zfs, env.src, dst)
    assert (dst / "docs").stat().st_mtime_ns == EARLY_NS


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
    assert filemove_env.sim.props[STAGED, MARK_KEY] == "prompt"


def test_check_passphrase_file_location_not_attached(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Check passphrase file location not attached."""
    settings = replace(settings, final_keylocation="file:///keys/final")
    check_passphrase(staged_env(filemove_env).zfs, settings, STAGED)
    assert filemove_env.runner.attached == []
    assert ("zfs", "load-key", "-n", "-L", "file:///keys/final", STAGED) in filemove_env.runner.calls
    assert filemove_env.sim.props[STAGED, MARK_KEY] == "file:///keys/final"


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
    staged_env(filemove_env).sim.prop(STAGED, MARK_KEY, "prompt")
    check_passphrase(filemove_env.zfs, settings, STAGED)
    assert filemove_env.runner.attached == []
    assert not [call for call in filemove_env.runner.calls if "load-key" in call]


def test_check_passphrase_again_for_another_location(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """The marker records which location was checked; a rerun with another final location checks that one."""
    env = staged_env(filemove_env)
    check_passphrase(env.zfs, replace(settings, final_keylocation="file:///a"), STAGED)
    assert env.sim.props[STAGED, MARK_KEY] == "file:///a"
    check_passphrase(env.zfs, replace(settings, final_keylocation="file:///b"), STAGED)
    assert [call for call in env.runner.calls if "load-key" in call] == [
        ("zfs", "load-key", "-n", "-L", "file:///a", STAGED),
        ("zfs", "load-key", "-n", "-L", "file:///b", STAGED),
    ]
    assert env.sim.props[STAGED, MARK_KEY] == "file:///b"


# --- move loop ---


def recorder() -> tuple[list[float], Callable[[float], None]]:
    """Return the list of sleeps and a fake sleep that records them."""
    slept: list[float] = []
    return slept, slept.append


def tamper_after_rsync(env: FileMoveEnv, action: Callable[[int], object]) -> None:
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
    """Run the move loop on the environment, with the staged dataset mounted at ``dst``."""
    env.sim.prop(STAGED, "mountpoint", str(dst))
    env.sim.prop(STAGED, "mounted", "yes")
    move_files(env.zfs, settings, DATASET, env.src, dst, batch_size, sleep, device=dst.stat().st_dev)


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
def test_moves_names_rsync_reads_as_comments(filemove_env: FileMoveEnv, settings: Settings, tmp_path: Path) -> None:
    """Names starting with # or ; are copied: rsync skips such --files-from lines as comments."""
    env = filemove_env
    (env.src / "#x").write_text("hash")
    (env.src / ";y").write_text("semi")
    (env.src / "#dir").mkdir()
    (env.src / "#dir" / "f").write_text("in hash dir")
    os.link(env.src / ";y", env.src / "#dir" / "y-link")
    before = contents(env.src)
    dst = tmp_path / "dst"
    dst.mkdir()
    move(env, settings, dst, 10**9, recorder()[1])
    assert source_files(env) == []
    assert {name: text for name, text in contents(dst).items() if not name.startswith(SCRATCH)} == before
    assert (dst / ";y").stat().st_ino == (dst / "#dir" / "y-link").stat().st_ino


SNAPSHOT_REFUSAL = f"{DATASET} has snapshots, which keep deleted files allocated: {DATASET}@auto"
SNAPSHOT_HINTS = (f"disable snapshot tools for {DATASET}, then remove them: zfs destroy {DATASET}@auto",)


@needs_rsync
def test_same_second_same_size_change_is_copied_again(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """A copy left by an earlier run that matches in size and second, but not nanosecond, is copied again.

    rsync's quick check would otherwise skip it, and verify would refuse the mtime on every rerun.
    """
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "a.txt").write_text("z")
    os.utime(dst / "a.txt", ns=(LATE_NS, LATE_NS))
    os.utime(env.src / "a.txt", ns=(EARLY_NS, EARLY_NS))
    move(env, settings, dst, 10**9, recorder()[1])
    assert source_files(env) == []
    assert (dst / "a.txt").read_text() == "a"
    assert (dst / "a.txt").stat().st_mtime_ns == EARLY_NS


@needs_rsync
def test_snapshot_appearing_mid_run_stops_before_next_batch(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot taken after a batch was deleted stops the run before the next batch copies; the batch stays moved."""
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    real_delete = filetree.delete

    def delete_then_snapshot(src: Path, groups: Sequence[Group]) -> int:
        count = real_delete(src, groups)
        env.sim.snapshot(f"{DATASET}@auto")
        return count

    monkeypatch.setattr(filemove, "delete", delete_then_snapshot)
    with pytest.raises(MigrationError) as info:
        move(env, settings, dst, 1, recorder()[1])
    assert info.value.message == SNAPSHOT_REFUSAL
    assert info.value.hints == SNAPSHOT_HINTS
    moved = sorted(str(p.relative_to(dst)) for p in dst.rglob("*.txt"))
    assert len(moved) == 1
    assert len(source_files(env)) == 2  # noqa: PLR2004
    assert not (env.src / moved[0]).exists()


@needs_rsync
def test_staged_unmounted_between_batches_stops_before_copying(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the staged dataset unmounted, its mountpoint is a bare directory: copies there would vanish.

    The run stops before the next batch copies anything, so no source file is deleted against such a copy.
    """
    env = filemove_env
    real_delete = filetree.delete
    hidden = tmp_path / "hidden"

    def delete_then_unmount(src: Path, groups: Sequence[Group]) -> int:
        count = real_delete(src, groups)
        if not hidden.exists():
            # What zfs unmount does: the copies go with the dataset, an empty directory stays behind.
            env.zfs.unmount(STAGED)
            staged_dir(env).rename(hidden)
            staged_dir(env).mkdir()
        return count

    monkeypatch.setattr(filemove, "delete", delete_then_unmount)
    with pytest.raises(MigrationError) as info:
        run_file_move(env, settings, tmp_path)
    unmounted = f"{STAGED} is no longer mounted at {staged_dir(env)}; nothing of this batch was deleted"
    assert info.value.message == unmounted
    assert len(source_files(env)) == 2  # noqa: PLR2004
    assert [p for p in staged_dir(env).rglob("*") if p.is_file()] == []


@needs_rsync
def test_staged_unmounted_between_passes_deletes_nothing_more(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that starts after the staged dataset was unmounted copies nothing into the bare directory left behind.

    Without the check the pass recreated scratch there, copied, verified against that copy and deleted the source.
    """
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    hidden = tmp_path / "hidden"
    # A changed file is skipped by the first pass, so a second pass follows.
    tamper_after_rsync(env, lambda n: (env.src / "a.txt").write_text("changed") if n == 1 else None)
    real_delete = filetree.delete

    def delete_then_unmount(src: Path, groups: Sequence[Group]) -> int:
        count = real_delete(src, groups)
        if not hidden.exists():
            env.zfs.unmount(STAGED)
            dst.rename(hidden)
            dst.mkdir()
        return count

    monkeypatch.setattr(filemove, "delete", delete_then_unmount)
    with pytest.raises(MigrationError) as info:
        move(env, settings, dst, 10**9, recorder()[1])
    assert info.value.message == f"{STAGED} is no longer mounted at {dst}; nothing of this batch was deleted"
    assert source_files(env) == ["a.txt"]
    assert list(dst.iterdir()) == []


@needs_rsync
def test_snapshot_taken_during_copy_keeps_the_batch(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """A snapshot taken while a batch copies stops the run before that batch is deleted."""
    env = filemove_env
    dst = tmp_path / "dst"
    dst.mkdir()
    before = source_files(env)
    tamper_after_rsync(env, lambda n: env.sim.snapshot(f"{DATASET}@auto") if n == 1 else None)
    with pytest.raises(MigrationError) as info:
        move(env, settings, dst, 1, recorder()[1])
    assert info.value.message == SNAPSHOT_REFUSAL
    assert info.value.hints == SNAPSHOT_HINTS
    assert source_files(env) == before
    assert len(list(dst.rglob("*.txt"))) == 1


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


def test_move_removes_partial_files_before_the_space_check(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Temp files of an rsync killed by a power loss or SIGKILL are removed first; the pristine copy stays."""
    env = filemove_env
    dst = tmp_path / "dst"
    temp = dst / SCRATCH / "tmp"
    temp.mkdir(parents=True)
    (temp / ".a.txt.Xy12Zq").write_bytes(b"partial")
    (dst / SCRATCH / "dirs" / "docs").mkdir(parents=True)
    env.sim.avail = 0
    # The first space check, the admission: nothing fits.
    with pytest.raises(MigrationError, match="does not cover the worst case of this move"):
        move(env, settings, dst, 10**9, recorder()[1])
    assert not temp.exists() or list(temp.iterdir()) == []
    assert (dst / SCRATCH / "dirs" / "docs").is_dir()


SIZES = {"a.txt": 1000, "docs/b.txt": 9000, "docs/deep/c.txt": 1000}


def fixed_sizes(root: Path) -> list[filetree.Entry]:
    """Scan with the allocations of SIZES: a fresh file's own allocation changes once the pool writes it out."""
    return [replace(item, allocated=SIZES[item.name]) for item in filetree.scan(root)]


ADMISSION = [
    # (case, batch size, pool properties, source properties, largest batch or file, cloned, lost compression)
    ("batch", 20_000, {}, {}, 20_000, 0, 0),
    ("file", 1, {}, {}, 9000, 0, 0),
    ("cloned", 1, {"bclonesaved": "3000"}, {}, 9000, 3000, 0),
    ("uncompressed", 1, {}, {"compression": "off", "logicalreferenced": "50000", "referenced": "48000"}, 9000, 0, 2000),
    ("compressed", 1, {}, {"compression": "lz4", "logicalreferenced": "50000", "referenced": "48000"}, 9000, 0, 0),
]


@needs_rsync
@pytest.mark.parametrize("spare", [0, -1])
@pytest.mark.parametrize(
    ("batch_size", "pool_props", "source_props", "largest", "cloned", "loss"),
    [case[1:] for case in ADMISSION],
    ids=[case[0] for case in ADMISSION],
)
def test_admission_needs_room_for_the_worst_case(  # noqa: PLR0917
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    batch_size: int,
    pool_props: dict[str, str],
    source_props: dict[str, str],
    largest: int,
    cloned: int,
    loss: int,
    spare: int,
) -> None:
    """Before the move deletes anything, free space must cover its worst case, or it could stall half-moved.

    The largest batch or file (deleting others frees only what their copies take), block-cloned data (a copy
    of a clone takes its full size) and, with compression now off, what the old compressed data grows by.
    """
    env = filemove_env
    monkeypatch.setattr(filemove, "scan", fixed_sizes)
    for prop, value in pool_props.items():
        env.sim.pool_props["tank", prop] = value
    for prop, value in source_props.items():
        env.sim.prop(DATASET, prop, value)
    need = largest + cloned + loss
    env.sim.avail = settings.min_free + need + spare
    dst = tmp_path / "dst"
    dst.mkdir()
    before = source_files(env)
    if spare >= 0:
        move(env, settings, dst, batch_size, recorder()[1])
        assert source_files(env) == []
        assert env.sim.props[STAGED, MARK_ADMITTED] == "yes"
        return
    with pytest.raises(MigrationError) as info:
        move(env, settings, dst, batch_size, recorder()[1])
    assert info.value.message == (
        f"free space on tank above the floor ({format_size(need + spare)}) does not cover the worst case of this "
        f"move ({format_size(need)}: largest batch or file {format_size(largest)}, block-cloned data "
        f"{format_size(cloned)}, lost compression {format_size(loss)})"
    )
    assert source_files(env) == before
    assert not [call for call in env.runner.calls if call[0] == "rsync"]
    assert (STAGED, MARK_ADMITTED) not in env.sim.props


def test_admission_reads_no_block_cloning_as_none(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """An OpenZFS without block cloning (before 2.2) has no bclonesaved, and nothing cloned."""
    env = filemove_env

    def no_such_property(args: Sequence[str]) -> CommandResult | None:
        if tuple(args[:2]) == ("zpool", "get") and "bclonesaved" in args:
            return fail("bad property list: invalid property 'bclonesaved'\n", 2)
        return None

    zfs, _ = make(env.sim, no_such_property)
    env.sim.avail = settings.min_free + 9000
    admit(zfs, settings, DATASET, filetree.group_by_inode(fixed_sizes(env.src), env.src), 1)
    assert env.sim.props[STAGED, MARK_ADMITTED] == "yes"


@needs_rsync
def test_rerun_after_admission_does_not_check_again(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copies a stopped run left behind use the space the admission counted on; the rerun is not refused for it."""
    env = filemove_env
    monkeypatch.setattr(filemove, "scan", fixed_sizes)
    dst = tmp_path / "dst"
    dst.mkdir()
    groups = filetree.group_by_inode(filetree.scan(env.src), env.src)
    filetree.write_file_list(tmp_path / "list", groups)
    rsync_files(env.zfs, env.src, dst, tmp_path / "list")
    env.sim.prop(STAGED, MARK_ADMITTED, "yes")
    env.sim.pool_props["tank", "bclonesaved"] = "3000"
    env.sim.avail = settings.min_free
    move(env, settings, dst, 1, recorder()[1])
    assert source_files(env) == []


@needs_rsync
def test_copies_left_by_a_stop_need_no_space(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch whose copies a stopped run left behind waits only for the space of the files not copied yet.

    Budgeting the whole batch again would keep a run near the floor from ever reaching verify and delete.
    """
    env = filemove_env
    size = 4096
    # A fixed size per file: a fresh file's allocation changes once the pool writes it out.
    monkeypatch.setattr(filemove, "scan", lambda root: [replace(item, allocated=size) for item in filetree.scan(root)])
    dst = tmp_path / "dst"
    dst.mkdir()
    groups = filetree.group_by_inode(filetree.scan(env.src), env.src)
    filetree.write_file_list(tmp_path / "list", [g for g in groups if g.first != "docs/deep/c.txt"])
    rsync_files(env.zfs, env.src, dst, tmp_path / "list")
    # Room for c.txt, the one file not copied yet, but not for the whole batch of three. The stopped run that
    # left the copies had passed the admission.
    env.sim.avail = settings.min_free + size
    env.sim.prop(STAGED, MARK_ADMITTED, "yes")
    slept, sleep = recorder()
    move(env, settings, dst, 10**9, sleep)
    assert source_files(env) == []
    assert slept == []


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


# --- swap and the composed command ---

FINAL = "file:///keys/final"
SPLIT_NOTES = (
    f"file-move stopped; the files are split between {DATASET} and {STAGED}; do not destroy either.",
    "Rerun the same command to continue.",
)
_REAL = SubprocessRunner()


def real_rsync(args: Sequence[str]) -> CommandResult | None:
    """Run rsync for real and leave every other command to the simulator."""
    return _REAL.run(args) if args[0] == "rsync" else None


def run_file_move(
    env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    *,
    zfs: Zfs | None = None,
    batch_size: int = 1,
) -> list[str]:
    """Run the whole command with a final key location that needs no terminal."""
    return file_move(
        zfs or env.zfs,
        replace(settings, final_keylocation=FINAL),
        DATASET,
        batch_size=batch_size,
        delete_source=True,
        mount_root=env.mount_root,
        lock_path=tmp_path / "lock",
        sleep=recorder()[1],
    )


def staged_dir(env: FileMoveEnv) -> Path:
    """Directory the simulator 'mounts' the staged dataset on; the copy stays there after the swap."""
    return env.mount_root / "media"


def dir_times(root: Path) -> dict[str, int]:
    """Directory mtimes under ``root``, keyed by relative path."""
    dirs = [root, *(p for p in root.rglob("*") if p.is_dir() and not p.is_symlink())]
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in dirs}


def contents(root: Path) -> dict[str, str]:
    """Text of every regular file under ``root``, keyed by relative path."""
    return {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def set_dir_times(root: Path) -> None:
    """Give every directory a distinct whole-second mtime, after the test changed them."""
    for index, directory in enumerate(sorted(dir_times(root), reverse=True)):
        os.utime(root / directory, (1_100_000_000 + index, 1_100_000_000 + index))


def assert_in_order(ops: list[tuple[str, ...]], expected: list[tuple[str, ...]]) -> None:
    """Each expected command appears in ``ops`` after the previous one."""
    position = 0
    for op in expected:
        assert op in ops[position:], f"{op} missing after position {position}"
        position = ops.index(op, position) + 1


def written_read(name: str) -> tuple[str, ...]:
    """Return the read of written@enc-final."""
    return ("zfs", "get", "-H", "-p", "-o", "value", "written@enc-final", name)


def rename_final_aside(env: FileMoveEnv) -> None:
    """Do what the stale enc-final hint says: rename it aside."""
    env.sim.snapshots[f"{DATASET}@enc-final-1"] = env.sim.snapshots.pop(f"{DATASET}@enc-final")


@needs_rsync
def test_file_move_end_to_end(filemove_env: FileMoveEnv, settings: Settings, tmp_path: Path) -> None:
    """The files end in the encrypted dataset, swapped into place with the source's properties and times."""
    env = filemove_env
    os.link(env.src / "a.txt", env.src / "docs" / "a-link.txt")
    (env.src / "link").symlink_to("a.txt")
    set_dir_times(env.src)
    times = dir_times(env.src)
    before = contents(env.src)
    env.sim.inherited[DATASET, "compression"] = ("lz4", "tank")

    hints = run_file_move(env, settings, tmp_path)

    assert hints == [
        f"verify the key with: zfs load-key -n {DATASET}",
        "remount NFS clients: the new dataset has new file handles",
        f"re-enable snapshot tools for {DATASET}; replication restarts with a full send",
        f"file-move done; once verified, free the old copy with: zfs destroy -r {STAGE}/old/media",
    ]
    dst = staged_dir(env)
    assert source_files(env) == []
    assert contents(dst) == before
    assert (dst / "a.txt").stat().st_ino == (dst / "docs" / "a-link.txt").stat().st_ino
    assert (dst / "link").readlink() == Path("a.txt")
    assert not (dst / SCRATCH).exists()
    assert dir_times(dst) == times
    sim = env.sim
    assert STAGED not in sim.datasets
    assert f"{STAGE}/old/media" in sim.datasets
    assert sim.props[f"{STAGE}/old/media", "readonly"] == "on"
    assert f"{STAGE}/old/media@enc-final" in sim.snapshots
    assert sim.props[DATASET, "encryption"] == "aes-256-gcm"
    assert sim.props[DATASET, "keylocation"] == FINAL
    assert sim.props[DATASET, "mountpoint"] == str(env.src)
    assert sim.props[DATASET, "canmount"] == "on"
    assert sim.props[DATASET, "mounted"] == "yes"
    assert sim.props[DATASET, "keystatus"] == "available"
    assert [prop for name, prop in sim.props if name == DATASET and prop.startswith("zfs-encrypt-migrate:")] == []
    # Pinned for the copy, inherited again like on the source.
    assert (DATASET, "compression") not in sim.props
    assert_in_order(
        sim.ops(),
        [
            ("zfs", "mount", "-o", "noatime", DATASET),
            ("zfs", "set", f"{MARK_ADMITTED}=yes", STAGED),
            ("zfs", "snapshot", f"{DATASET}@enc-final"),
            ("zfs", "unmount", DATASET),
            ("zpool", "sync", "tank"),
            written_read(DATASET),
            ("zfs", "unmount", STAGED),
            ("zfs", "set", "canmount=noauto", DATASET),
            ("zfs", "rename", "-u", DATASET, f"{STAGE}/old/media"),
            ("zfs", "rename", "-u", STAGED, DATASET),
            ("zfs", "set", f"keylocation={FINAL}", DATASET),
            ("zfs", "set", f"mountpoint={env.src}", DATASET),
            ("zfs", "inherit", "compression", DATASET),
            ("zfs", "inherit", MARK_SOURCE, DATASET),
            ("zfs", "inherit", MARK_DIRS, DATASET),
            ("zfs", "inherit", MARK_KEY, DATASET),
            ("zfs", "inherit", MARK_ADMITTED, DATASET),
            ("zfs", "set", "canmount=on", DATASET),
            ("zfs", "mount", DATASET),
        ],
    )


def test_file_move_needs_delete_source(filemove_env: FileMoveEnv, settings: Settings, tmp_path: Path) -> None:
    """Without --delete-source nothing runs, not even the lock."""
    env = filemove_env
    with pytest.raises(MigrationError) as info:
        file_move(
            env.zfs,
            settings,
            DATASET,
            batch_size=1,
            delete_source=False,
            mount_root=env.mount_root,
            lock_path=tmp_path / "lock",
        )
    assert info.value.message == (
        "file-move deletes each source file once its encrypted copy is verified; pass --delete-source to confirm"
    )
    assert env.runner.calls == []
    assert not (tmp_path / "lock").exists()


def test_finish_refuses_stale_enc_final(filemove_env: FileMoveEnv, settings: Settings, tmp_path: Path) -> None:
    """A leftover enc-final is renamed aside by hand, never reused."""
    env = filemove_env
    env.sim.snapshot(f"{DATASET}@enc-final")
    with pytest.raises(MigrationError) as info:
        finish(env.zfs, settings, DATASET, env.src, tmp_path / "dst", guard_for(env, settings))
    assert info.value.message == (
        f"{DATASET}@enc-final already exists; rename it aside with: zfs rename {DATASET}@enc-final @enc-final-1"
    )
    assert env.sim.ops() == []


FREE_THEN_SNAPSHOT = [
    ("zfs", "unmount", DATASET),
    ("zfs", "mount", "-o", "noatime", DATASET),
    ("zpool", "sync", "tank"),
    ("zfs", "snapshot", f"{DATASET}@enc-final"),
]


def test_finish_refuses_busy_source_before_enc_final(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """A writer that holds the source at the end stops the swap before enc-final is taken."""
    env = filemove_env
    env.sim.busy.add(DATASET)
    with pytest.raises(MigrationError) as info:
        finish(env.zfs, settings, DATASET, env.src, tmp_path / "dst", guard_for(env, settings))
    assert info.value.message == f"cannot unmount {DATASET}; a writer is probably still running"
    assert env.sim.ops() == [("zfs", "unmount", DATASET)]


def test_finish_refuses_files_appearing(filemove_env: FileMoveEnv, settings: Settings, tmp_path: Path) -> None:
    """Files found after the final snapshot stop the swap before anything else changes."""
    env = filemove_env
    with pytest.raises(MigrationError) as info:
        finish(env.zfs, settings, DATASET, env.src, tmp_path / "dst", guard_for(env, settings))
    assert info.value.message == f"3 files appeared in {DATASET} during the swap; stop the writers and rerun"
    assert env.sim.ops() == FREE_THEN_SNAPSHOT
    assert not [call for call in env.runner.calls if call[0] == "rsync"]


def moved(env: FileMoveEnv, settings: Settings, guard: AbortGuard) -> Path:
    """Open the staged dataset, copy the tree and move every file; return the staged path."""
    dst, device = open_staged(env.zfs, settings, DATASET, env.zfs.props(DATASET), env.mount_root, guard)
    copy_tree(env.zfs, STAGED, env.src, dst, device)
    move(env, settings, dst, 10**9, recorder()[1])
    return dst


@needs_rsync
def test_finish_refuses_written_after_scan(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """A write after the final scan stops the swap with the rename-aside hint; the staged key is unloaded."""
    env = filemove_env
    guard = AbortGuard(env.zfs, settings, STAGED, source=None, leaf=None, label="file-move")
    guard.notes = list(SPLIT_NOTES)
    dst = moved(env, settings, guard)
    env.sim.prop(DATASET, "written@enc-final", "4096")
    with pytest.raises(MigrationError) as info, guard:
        finish(env.zfs, settings, DATASET, env.src, dst, guard)
    assert info.value.message == f"{DATASET} was written after the final scan"
    assert info.value.hints == (
        "rename the stale snapshot aside, then rerun file-move:",
        f"  zfs rename {DATASET}@enc-final @enc-final-1",
        "file-move aborted.",
        f"{DATASET} may be unmounted; remount with: zfs mount -a",
    )
    assert not [op for op in env.sim.ops() if op[1] == "rename"]
    assert env.sim.props[STAGED, "mounted"] == "no"
    assert env.sim.props[STAGED, "keystatus"] == "unavailable"
    # Kept for the rerun, which restores the directory times again.
    assert (dst / SCRATCH / "dirs").is_dir()


@needs_rsync
def test_finish_refuses_busy_staged(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """A busy staged dataset stops the swap; the guard keeps the key loaded and says how to finish."""
    env = filemove_env
    guard = AbortGuard(env.zfs, settings, STAGED, source=None, leaf=None, label="file-move")
    dst = moved(env, settings, guard)
    env.sim.busy.add(STAGED)
    with pytest.raises(MigrationError) as info, guard:
        finish(env.zfs, settings, DATASET, env.src, dst, guard)
    assert info.value.message == f"cannot unmount {STAGED}; something still uses {dst}"
    assert info.value.hints[0] == f"could not unmount {STAGED}; run: zfs unmount {STAGED} && zfs unload-key -r {STAGED}"
    assert not [op for op in env.sim.ops() if op[1] in {"rename", "inherit"} or op[2].startswith("mountpoint=")]


@needs_rsync
def test_finish_frees_last_deletes_before_enc_final(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Unmount, noatime remount and sync come before enc-final.

    The unmount waits for the last batch's deletes to be freed, so they never count in written@enc-final.
    """
    env = filemove_env
    guard = AbortGuard(env.zfs, settings, STAGED, source=None, leaf=None, label="file-move")
    dst = moved(env, settings, guard)
    env.sim.log.clear()
    finish(env.zfs, settings, DATASET, env.src, dst, guard)
    assert env.sim.ops()[:4] == FREE_THEN_SNAPSHOT


@needs_rsync
def test_post_swap_fixups_never_touch_staged(filemove_env: FileMoveEnv, settings: Settings) -> None:
    """Fix-ups act on the swapped dataset only, so a stopped swap never leaves the copy with the source's mountpoint."""
    env = filemove_env
    guard = AbortGuard(env.zfs, settings, STAGED, source=None, leaf=None, label="file-move")
    dst = moved(env, settings, guard)
    env.sim.log.clear()
    finish(env.zfs, settings, DATASET, env.src, dst, guard)
    assert [op for op in env.sim.ops() if op[-1] == STAGED and op[1] in {"set", "inherit"}] == []


@needs_rsync
def test_interrupt_during_rsync_cleans_up_and_rerun_completes(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Ctrl-C in a batch unmounts the staged dataset, then unloads its key; a rerun moves the rest."""
    env = filemove_env
    times = dir_times(env.src)
    before = contents(env.src)
    file_rsyncs = 0

    def interrupt_second_batch(args: Sequence[str]) -> CommandResult | None:
        nonlocal file_rsyncs
        if args[0] == "rsync" and "-aHAXS" in args:
            file_rsyncs += 1
            if file_rsyncs == 2:  # noqa: PLR2004
                raise KeyboardInterrupt
        return real_rsync(args)

    zfs, _ = make(env.sim, interrupt_second_batch)
    with pytest.raises(Interrupted) as info:
        run_file_move(env, settings, tmp_path, zfs=zfs)
    assert info.value.hints == SPLIT_NOTES
    assert env.sim.ops()[-2:] == [("zfs", "unmount", STAGED), ("zfs", "unload-key", "-r", STAGED)]
    assert len(source_files(env)) == 2  # noqa: PLR2004

    zfs, _ = make(env.sim, real_rsync)
    run_file_move(env, settings, tmp_path, zfs=zfs)
    assert source_files(env) == []
    assert contents(staged_dir(env)) == before
    assert dir_times(staged_dir(env)) == times
    assert env.sim.props[DATASET, "encryption"] == "aes-256-gcm"


@needs_rsync
def test_rerun_after_interrupt_mid_delete_keeps_links(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group whose first name was already deleted is verified and deleted on the rerun without a new copy."""
    env = filemove_env
    first, second = env.src / "a.txt", env.src / "docs" / "a-link.txt"
    os.link(first, second)
    unlink = os.unlink

    def interrupt_between_names(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        if Path(path) == second and not first.exists():
            raise KeyboardInterrupt
        unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    # filetree.delete unlinks through os.unlink (Path.unlink calls it).
    monkeypatch.setattr(os, "unlink", interrupt_between_names)
    with pytest.raises(Interrupted):
        run_file_move(env, settings, tmp_path, batch_size=10**9)
    # Only os.unlink: undo() would also drop the fixture's mount stand-in.
    monkeypatch.setattr(os, "unlink", unlink)
    dst = staged_dir(env)
    assert source_files(env) == ["docs/a-link.txt", "docs/b.txt", "docs/deep/c.txt"]
    inode = (dst / "a.txt").stat().st_ino
    assert (dst / "docs" / "a-link.txt").stat().st_ino == inode

    run_file_move(env, settings, tmp_path, batch_size=10**9)
    assert source_files(env) == []
    assert (dst / "a.txt").stat().st_ino == inode
    assert (dst / "docs" / "a-link.txt").stat().st_ino == inode
    assert (dst / "a.txt").stat().st_nlink == 2  # noqa: PLR2004
    assert (dst / "a.txt").read_text() == "a"


@needs_rsync
def test_failure_after_swap_prints_canmount_and_mount(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """After the renames the hints also give the marker, canmount and mount commands."""
    env = filemove_env

    def fail_final_mount(args: Sequence[str]) -> CommandResult | None:
        # The writer probe mounts the plain source too; only the swapped, encrypted one fails.
        if tuple(args) == ("zfs", "mount", DATASET) and env.sim.props.get((DATASET, "encryption")) == "aes-256-gcm":
            return fail("injected\n")
        return real_rsync(args)

    zfs, _ = make(env.sim, fail_final_mount)
    with pytest.raises(MigrationError) as info:
        run_file_move(env, settings, tmp_path, zfs=zfs)
    assert info.value.hints == swapped_hints(env)
    assert env.sim.props[DATASET, "keystatus"] == "available"


def swapped_hints(env: FileMoveEnv) -> tuple[str, ...]:
    """Hints of a failure once the renames are done: finish keylocation, fix-ups, markers, canmount, mount."""
    return (
        "file-move aborted.",
        "the swap completed; only the final steps failed. Finish with:",
        f"  zfs set keylocation={FINAL} {DATASET}",
        "  zfs mount -a",
        f"  zfs set mountpoint={env.src} {DATASET}",
        # Every write-affecting property the source inherits or defaults; the simulator lists only dedup.
        f"  zfs inherit dedup {DATASET}",
        f"  zfs inherit {MARK_SOURCE} {DATASET}",
        f"  zfs inherit {MARK_DIRS} {DATASET}",
        f"  zfs inherit {MARK_KEY} {DATASET}",
        f"  zfs inherit {MARK_ADMITTED} {DATASET}",
        f"  zfs set canmount=on {DATASET}",
        f"  zfs mount {DATASET}",
    )


@needs_rsync
def test_failure_inside_swap_hints_post_swap_commands(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The post-swap commands are hinted before the swap runs, so a failure inside it still gives them."""
    env = filemove_env

    def fail_keylocation(args: Sequence[str]) -> CommandResult | None:
        if tuple(args) == ("zfs", "set", f"keylocation={FINAL}", DATASET):
            return fail("injected\n")
        return real_rsync(args)

    zfs, _ = make(env.sim, fail_keylocation)
    with pytest.raises(MigrationError, match="injected") as info:
        run_file_move(env, settings, tmp_path, zfs=zfs)
    assert info.value.hints == swapped_hints(env)


@needs_rsync
def test_post_swap_hints_quote_values(filemove_env: FileMoveEnv, settings: Settings, tmp_path: Path) -> None:
    """A property value with a space stays one argument in the printed zfs set command."""
    env = filemove_env
    spaced = tmp_path / "my media"
    env.src.rename(spaced)
    env.sim.prop(DATASET, "mountpoint", str(spaced))

    def fail_keylocation(args: Sequence[str]) -> CommandResult | None:
        if tuple(args) == ("zfs", "set", f"keylocation={FINAL}", DATASET):
            return fail("injected\n")
        return real_rsync(args)

    zfs, _ = make(env.sim, fail_keylocation)
    with pytest.raises(MigrationError, match="injected") as info:
        run_file_move(env, settings, tmp_path, zfs=zfs)
    assert f"  zfs set 'mountpoint={spaced}' {DATASET}" in info.value.hints


@needs_rsync
def test_rerun_moving_late_files_leaves_no_scratch(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """A file written during a refused swap is moved by the rerun; no scratch is left and the times are original."""
    env = filemove_env
    times = dir_times(env.src)

    def late_writer(args: Sequence[str]) -> CommandResult | None:
        result = real_rsync(args)
        if args[0] == "rsync" and args[-2].endswith(f"{SCRATCH}/dirs/"):
            (env.src / "docs" / "late.txt").write_text("late")
            env.sim.busy.add(DATASET)
        return result

    zfs, _ = make(env.sim, late_writer)
    with pytest.raises(MigrationError) as info:
        run_file_move(env, settings, tmp_path, zfs=zfs)
    assert info.value.message == f"cannot unmount {DATASET}; a writer is probably still running"
    assert info.value.hints == ("file-move aborted.", f"{DATASET} may be unmounted; remount with: zfs mount -a")

    env.sim.busy.clear()
    del env.sim.snapshots[f"{DATASET}@enc-final"]
    run_file_move(env, settings, tmp_path)
    dst = staged_dir(env)
    assert source_files(env) == []
    assert (dst / "docs" / "late.txt").read_text() == "late"
    assert not (dst / SCRATCH).exists()
    assert dir_times(dst) == times
    assert env.sim.props[DATASET, "encryption"] == "aes-256-gcm"


@needs_rsync
def test_stop_while_scratch_is_removed_keeps_dir_times(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop part-way through removing scratch leaves no partial pristine copy for the rerun to restore from.

    The rerun keeps the times the first finish restored, the root's included.
    """
    env = filemove_env
    times = dir_times(env.src)
    scratch = staged_dir(env) / SCRATCH
    real_rmtree = shutil.rmtree
    stopped = False

    def stop_part_way(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        nonlocal stopped
        if not stopped and Path(path).name != "tmp" and Path(path).is_relative_to(scratch):
            stopped = True
            pristine = next(child for child in scratch.iterdir() if child.name.startswith("dirs"))
            real_rmtree(pristine / "docs")
            raise KeyboardInterrupt
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "rmtree", stop_part_way)
    with pytest.raises(Interrupted):
        run_file_move(env, settings, tmp_path)
    assert stopped
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)

    # What the hints say: mount the source again, rename enc-final aside.
    env.sim.prop(DATASET, "mounted", "yes")
    rename_final_aside(env)
    run_file_move(env, settings, tmp_path)
    assert dir_times(staged_dir(env)) == times
    assert not scratch.exists()


@needs_rsync
def test_rerun_after_scratch_removed_keeps_dir_times(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """A stop after the pristine copy is removed, then a rerun, keeps the original directory times.

    By then the live directories carry the times the deletes changed, so the rerun must not copy them again.
    """
    env = filemove_env
    times = dir_times(env.src)

    def busy_staged(args: Sequence[str]) -> CommandResult | None:
        if tuple(args) == ("zfs", "unmount", STAGED):
            return fail(f"cannot unmount '{STAGED}': pool or dataset is busy\n")
        return real_rsync(args)

    zfs, _ = make(env.sim, busy_staged)
    with pytest.raises(MigrationError) as info:
        run_file_move(env, settings, tmp_path, zfs=zfs)
    dst = staged_dir(env)
    assert info.value.message == f"cannot unmount {STAGED}; something still uses {dst}"
    assert not (dst / SCRATCH).exists()

    # What the hints say: unmount the copy and unload its key, mount the source again, rename enc-final aside.
    env.sim.prop(STAGED, "mounted", "no")
    env.sim.prop(STAGED, "keystatus", "unavailable")
    env.sim.prop(DATASET, "mounted", "yes")
    rename_final_aside(env)
    run_file_move(env, settings, tmp_path)
    assert dir_times(dst) == times
    assert env.sim.props[DATASET, "encryption"] == "aes-256-gcm"


@needs_rsync
def test_staged_gets_the_atime_the_noatime_remount_hides(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The writer probe remounts the source with noatime, which zfs then reports as atime=off (temporary).

    The staged dataset must still be created with the source's own atime, read while the source was unmounted.
    """
    env = filemove_env
    env.sim.prop(DATASET, "atime", "on")
    run_file_move(env, settings, tmp_path)
    create = next(call for call in env.sim.log if call[1] == "create" and call[-1] == STAGED)
    assert "atime=on" in create


@needs_rsync
@pytest.mark.parametrize("where", ["equal", "inside", "contains"])
def test_refuses_staged_mounted_over_source(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    where: str,
) -> None:
    """A staged mount that overlaps the source stops the run before any file is copied or deleted."""
    env = filemove_env
    mountpoint = {"equal": env.src, "inside": env.src / "sub", "contains": env.src.parent}[where]
    env.sim.datasets.add(STAGED)
    env.sim.prop(STAGED, MARK_SOURCE, DATASET)
    env.sim.prop(STAGED, "mountpoint", str(mountpoint))
    before = source_files(env)
    with pytest.raises(MigrationError) as info:
        run_file_move(env, settings, tmp_path)
    assert info.value.message == f"{STAGED} is mounted at {mountpoint}, which overlaps {DATASET} at {env.src}"
    assert info.value.hints == SPLIT_NOTES
    assert source_files(env) == before
    assert not [call for call in env.runner.calls if call[0] == "rsync"]
    assert env.sim.ops()[-2:] == [("zfs", "unmount", STAGED), ("zfs", "unload-key", "-r", STAGED)]
