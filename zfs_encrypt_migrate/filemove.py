"""Encrypt a dataset bigger than the pool's free space by moving its files into an encrypted copy."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shlex
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import (
    LOCK_FILE,
    MARK_DIRS,
    MARK_KEY,
    MARK_SOURCE,
    SCRATCH,
    SPACE_ATTEMPTS,
    SPACE_DELAY_SECONDS,
    STAGING_MOUNT_ROOT,
)
from .errors import Interrupted, MigrationError
from .filetree import (
    delete,
    group_by_inode,
    mount_device,
    plan_batches,
    remove_partials,
    remove_scratch,
    scan,
    temp_dir,
    verify,
    write_file_list,
)
from .migrate import (
    ENCRYPTION,
    FINAL_SNAPSHOT,
    abort_guard,
    check_key_file,
    ensure_container,
    in_flight,
    leaf_of,
    load_stage_key,
    pool_of,
    refuse_cross_pool,
    refuse_nested,
    refuse_stale,
    rename_aside_commands,
    swap_into_place,
)
from .output import console
from .props import create_props, fixups
from .sizes import format_size

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from .migrate import AbortGuard, Settings
    from .zfs import Prop, Zfs

NO_MOUNTPOINT = {"none", "legacy"}


@contextlib.contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive non-blocking lock so two file-moves never run at once."""
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            msg = f"another file-move is running (lock {path})"
            raise MigrationError(msg) from None
        yield


def _refuse_source(zfs: Zfs, dataset: str) -> Path:
    """Refuse a source dataset file-move cannot handle; return its mountpoint."""
    if not zfs.exists(dataset):
        msg = f"dataset {dataset} does not exist"
        raise MigrationError(msg)
    if zfs.get("type", dataset) == "volume":
        msg = f"{dataset} is a volume; file-move moves the files of a filesystem"
        raise MigrationError(msg)
    if zfs.children(dataset):
        msg = f"{dataset} has child datasets; use the tree commands, or file-move each child"
        raise MigrationError(msg)
    if zfs.get("encryption", dataset) != "off":
        msg = f"{dataset} is already encrypted"
        raise MigrationError(msg)
    if zfs.get("readonly", dataset) == "on":
        msg = f"{dataset} is read-only; file-move must delete its files"
        raise MigrationError(msg)
    mountpoint = zfs.get("mountpoint", dataset)
    if mountpoint in NO_MOUNTPOINT:
        msg = f"{dataset} has mountpoint {mountpoint}; file-move needs a mounted path"
        raise MigrationError(msg)
    if zfs.get("mounted", dataset) != "yes":
        msg = f"{dataset} is not mounted"
        raise MigrationError(msg)
    reserved = Path(mountpoint) / SCRATCH
    # The staged copy keeps its scratch files under this name, so the source must not use it, not even as a
    # dangling symlink.
    if os.path.lexists(reserved):
        msg = f"{reserved} exists; file-move needs that name for its own files"
        raise MigrationError(msg)
    return Path(mountpoint)


def _refuse_pinned_space(zfs: Zfs, dataset: str) -> None:
    """Refuse when deleting the source's files would not free their space: the move would stall half-way."""
    for prop in ("reservation", "refreservation"):
        value = zfs.get(prop, dataset)
        if value not in {"none", "0"}:
            msg = (
                f"{dataset} has {prop}={value}, which keeps deleted space reserved; clear it with: "
                f"zfs set {prop}=none {dataset} (set it again on the new dataset after the move)"
            )
            raise MigrationError(msg)
    origin = zfs.get("origin", dataset)
    if origin != "-":
        msg = (
            f"{dataset} is a clone of {origin}; its origin keeps the space of deleted files; "
            f"promote it first: zfs promote {dataset}"
        )
        raise MigrationError(msg)
    pool = pool_of(dataset)
    if zfs.pool_get("checkpoint", pool) != "-":
        msg = (
            f"pool {pool} has a checkpoint, which keeps deleted space allocated; "
            f"discard it first: zpool checkpoint -d {pool}"
        )
        raise MigrationError(msg)


def preflight_file_move(zfs: Zfs, settings: Settings, dataset: str) -> Path:
    """Refuse unless the run can start or resume; return the source mountpoint. Writes nothing."""
    check_key_file(settings)
    if "/" not in dataset:
        msg = f"{dataset} is a pool root; file-move is not supported"
        raise MigrationError(msg)
    refuse_cross_pool(settings, dataset, "file-move")
    refuse_nested(settings, dataset)
    source = _refuse_source(zfs, dataset)
    _refuse_pinned_space(zfs, dataset)
    # Here as well as before each batch, so a snapshot stops the run before anything is written. Only while
    # files are left: then a snapshot pins nothing, and a rerun after a refused swap keeps its renamed enc-final.
    if zfs.snapshots(dataset) and scan(source):
        _refuse_snapshots(zfs, dataset)
    stage = settings.stage
    staged = f"{stage}/{leaf_of(dataset)}"
    staged_exists = zfs.exists(staged)
    if staged_exists and zfs.get(MARK_SOURCE, staged) != dataset:
        msg = f"{staged} exists and is not a file-move of {dataset}"
        raise MigrationError(msg)
    if [name for name in in_flight(zfs, settings) if name != staged]:
        msg = f"a migration is already in flight under {stage}"
        raise MigrationError(msg)
    old = f"{stage}/old/{leaf_of(dataset)}"
    if not staged_exists and zfs.exists(old):
        msg = f"{old} already exists"
        raise MigrationError(msg)
    return source


def probe_writers(zfs: Zfs, dataset: str) -> dict[str, Prop]:
    """Unmount the source and mount it again with noatime: a busy dataset means a writer is still running.

    Return the source's properties, read while it is unmounted: once mounted with noatime, zfs reports
    atime as a temporary value and hides the dataset's own.
    """
    if not zfs.unmount(dataset):
        msg = f"cannot unmount {dataset}; a writer is probably still running"
        raise MigrationError(msg)
    # Anything that stops the run here leaves the source unmounted, so every way out gives the remount
    # command; an interrupt stays an interrupt, so the CLI still exits 130.
    hint = f"remount it with: zfs mount {dataset}"
    try:
        props = zfs.props(dataset)
        # Reads after enc-final must not update atimes: written@ would count them as writes.
        zfs.mount(dataset, ["noatime"])
    except MigrationError as exc:
        exc.hints = (*exc.hints, hint)
        raise
    except KeyboardInterrupt as exc:
        msg = "interrupted"
        raise Interrupted(msg, [hint]) from exc
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        raise MigrationError(msg, [hint]) from exc
    return props


def open_staged(  # noqa: PLR0917
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    source_props: Mapping[str, Prop],
    mount_root: Path,
    guard: AbortGuard,
) -> tuple[Path, int]:
    """Create the encrypted staged dataset on the first run; load its key and mount it.

    Return its mountpoint and the device of the mounted dataset, which every later copy must be on.
    """
    leaf = leaf_of(dataset)
    staged = f"{settings.stage}/{leaf}"
    if not zfs.exists(staged):
        ensure_container(zfs, settings.stage)
        ensure_container(zfs, f"{settings.stage}/old")
        props = {
            **create_props(source_props),
            "encryption": ENCRYPTION,
            "keyformat": settings.keyformat,
            "keylocation": f"file://{settings.key_file}",
            # Mounted only at a private path, never over the source's own mountpoint.
            "canmount": "noauto",
            "mountpoint": str(mount_root / leaf),
            MARK_SOURCE: dataset,
        }
        zfs.create(staged, props)
    load_stage_key(zfs, staged)
    # Registered before the mount, so a stop right after it still unmounts; the guard skips it while unmounted.
    guard.mounted.append(staged)
    if zfs.get("mounted", staged) != "yes":
        zfs.mount(staged)
    path = Path(zfs.get("mountpoint", staged))
    device = mount_device(path)
    if device is None:
        msg = f"{staged} is not mounted at {path}"
        raise MigrationError(msg)
    return path, device


# rsync compares times in whole seconds by default; a negative window (rsync 3.1.3+) adds the nanoseconds.
# Without it, a copy whose time is off by less than a second is skipped by the quick check, then refused by
# verify on every rerun, and the pristine pass cannot restore a directory time off by less than a second.
MODIFY_WINDOW = "--modify-window=-1"


def _rsync(zfs: Zfs, args: list[str]) -> None:
    result = zfs.runner.run(args)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        msg = f"{shlex.join(args)} failed: {detail}"
        raise MigrationError(msg)


def rsync_files(zfs: Zfs, src: Path, dst: Path, file_list: Path) -> None:
    """Copy the NUL-separated list of files, keeping hardlinks, ACLs, xattrs and sparseness."""
    temp = temp_dir(dst)
    temp.mkdir(parents=True, exist_ok=True)
    _rsync(
        zfs,
        [
            "rsync",
            "-aHAXS",
            "--numeric-ids",
            MODIFY_WINDOW,
            "--from0",
            f"--files-from={file_list}",
            f"--temp-dir={temp}",
            f"{src}/",
            f"{dst}/",
        ],
    )


def rsync_dirs(zfs: Zfs, src: Path, dst: Path) -> None:
    """Copy only the directory tree with its metadata."""
    _rsync(
        zfs,
        [
            "rsync",
            "-aAX",
            "--numeric-ids",
            MODIFY_WINDOW,
            "--exclude=/.zfs",
            "--include=*/",
            "--exclude=*",
            f"{src}/",
            f"{dst}/",
        ],
    )


def copy_tree(zfs: Zfs, staged: str, src: Path, dst: Path) -> None:
    """Copy the directories twice, the second time into scratch so later batches can restore their mtimes."""
    if zfs.get(MARK_DIRS, staged) == "copied":
        return
    rsync_dirs(zfs, src, dst)
    shadow = dst / SCRATCH / "dirs"
    shadow.mkdir(parents=True, exist_ok=True)
    rsync_dirs(zfs, src, shadow)
    zfs.set_prop(staged, MARK_DIRS, "copied")


def stdin_is_terminal() -> bool:
    """Return whether a person can type a passphrase."""
    return sys.stdin.isatty()


def check_passphrase(zfs: Zfs, settings: Settings, staged: str) -> None:
    """Prove the final key unlocks the staged dataset before any file is deleted.

    The marker records the location that was checked, so a rerun with another final location checks again.
    """
    location = settings.final_keylocation
    if zfs.get(MARK_KEY, staged) == location:
        return
    if location == "prompt":
        if not stdin_is_terminal():
            msg = "run file-move once from a terminal so it can check the passphrase"
            raise MigrationError(msg)
        loaded = zfs.check_key(staged, "prompt", attached=True)
    else:
        loaded = zfs.check_key(staged, location, attached=False)
    if not loaded:
        msg = f"the key for {staged} did not load from {location}; nothing was deleted"
        raise MigrationError(msg)
    zfs.set_prop(staged, MARK_KEY, location)


def wait_for_space(zfs: Zfs, settings: Settings, needed: int, sleep: Callable[[float], None]) -> None:
    """Wait until the batch fits above the free-space floor; deleted space is freed asynchronously."""
    pool = pool_of(settings.stage)
    for attempt in range(SPACE_ATTEMPTS):
        if zfs.pool_avail(pool) - needed >= settings.min_free:
            return
        if attempt < SPACE_ATTEMPTS - 1:
            zfs.pool_sync(pool)
            sleep(SPACE_DELAY_SECONDS)
    msg = (
        f"not enough free space on {pool} for the next batch ({format_size(needed)}) "
        f"above the floor {format_size(settings.min_free)}"
    )
    raise MigrationError(msg)


def _refuse_snapshots(zfs: Zfs, dataset: str) -> None:
    snaps = zfs.snapshots(dataset)
    if snaps:
        msg = f"{dataset} has snapshots, which keep deleted files allocated: {', '.join(snaps)}"
        hints = [f"disable snapshot tools for {dataset}, then remove them: zfs destroy {snap}" for snap in snaps]
        raise MigrationError(msg, hints)


def _refuse_unmounted(zfs: Zfs, staged: str, dst: Path, device: int) -> None:
    """Refuse unless the staged dataset is still the filesystem mounted at ``dst``.

    Once it is unmounted, ``dst`` is a bare directory: copies there pass verify, and the source files deleted
    against them are gone once the dataset mounts over them again or the host reboots.
    """
    if zfs.get("mounted", staged) != "yes" or mount_device(dst) != device:
        msg = f"{staged} is no longer mounted at {dst}; nothing of this batch was deleted"
        raise MigrationError(msg)


def move_files(  # noqa: PLR0917
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    src: Path,
    dst: Path,
    batch_size: int,
    sleep: Callable[[float], None],
    *,
    device: int,
) -> None:
    """Move every file into the copy in verified batches; delete a batch only after its copy is verified and synced.

    ``device`` is the device of the staged dataset mounted at ``dst``; every copy must be on it.
    """
    pool = pool_of(settings.stage)
    staged = f"{settings.stage}/{leaf_of(dataset)}"
    scratch = dst / SCRATCH
    # Before any space check, since they hold space; the run lock keeps every other run out of scratch.
    remove_partials(dst)
    batch_number = 0
    while entries := scan(src):
        left = sum(group.allocated for group in group_by_inode(entries, src))
        deleted = 0
        for batch in plan_batches(group_by_inode(entries, src), batch_size):
            # Before anything is written into dst: it may have become a bare directory since the last batch.
            _refuse_unmounted(zfs, staged, dst, device)
            # Created only when files remain: a rerun after finish removed scratch must not change the copy's root.
            scratch.mkdir(exist_ok=True)
            # Checked before every batch: a snapshot that appears mid-run would pin the deleted files.
            _refuse_snapshots(zfs, dataset)
            moved = sum(group.allocated for group in batch)
            # A stop between copy and delete leaves copies that need no new space; rsync skips them.
            missing = sum(group.allocated for group in batch if not os.path.lexists(dst / group.first))
            wait_for_space(zfs, settings, missing, sleep)
            write_file_list(scratch / "files", batch)
            rsync_files(zfs, src, dst, scratch / "files")
            verified, _changed = verify(src, dst, batch, device=device)
            zfs.pool_sync(pool)
            # And again right before deleting: a snapshot taken while the batch copied would pin it.
            _refuse_snapshots(zfs, dataset)
            deleted += delete(src, verified)
            batch_number += 1
            left -= moved
            files = sum(len(group.entries) for group in batch)
            console.print(f"batch {batch_number}: {files} files, {format_size(moved)} moved, {format_size(left)} left")
        if deleted == 0:
            msg = f"{len(entries)} files kept changing during the move; stop the writers and rerun"
            raise MigrationError(msg)


def finish(  # noqa: PLR0917
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    src: Path,
    dst: Path,
    guard: AbortGuard,
) -> list[str]:
    """Swap the copy into place once the source holds only directories; return the hints for the operator."""
    leaf = leaf_of(dataset)
    staged = f"{settings.stage}/{leaf}"
    refuse_stale(zfs, FINAL_SNAPSHOT, [dataset], recursive=False)
    # Unmounting finishes what the last batch's deletes left pending and the sync commits it, so it does not
    # count in written@enc-final; a busy source means a writer came back.
    probe_writers(zfs, dataset)
    zfs.pool_sync(pool_of(dataset))
    # Taken before the last scan, so written@ catches a writer between that scan and the unmount.
    zfs.snapshot(f"{dataset}@{FINAL_SNAPSHOT}", recursive=False)
    entries = scan(src)
    if entries:
        msg = f"{len(entries)} files appeared in {dataset} during the swap; stop the writers and rerun"
        raise MigrationError(msg)
    # The live pass creates directories added since the start; the pristine pass restores their original times.
    # Without the pristine copy, an earlier finish already ran both and removed it; the live directories now
    # carry the times the deletes changed, so copying them again would lose the restored ones.
    pristine = dst / SCRATCH / "dirs"
    if pristine.is_dir():
        rsync_dirs(zfs, src, dst)
        rsync_dirs(zfs, pristine, dst)
    # From here the copy holds everything, so the swap's own recovery hints replace the split-data note.
    guard.notes = []
    guard.source = dataset
    guard.leaf = leaf
    if not zfs.unmount(dataset):
        msg = f"cannot unmount {dataset}; a writer is probably still running"
        raise MigrationError(msg)
    zfs.pool_sync(pool_of(dataset))
    if zfs.get(f"written@{FINAL_SNAPSHOT}", dataset) != "0":
        commands = rename_aside_commands(zfs, FINAL_SNAPSHOT, [dataset], recursive=False)
        msg = f"{dataset} was written after the final scan"
        hints = ["rename the stale snapshot aside, then rerun file-move:", *(f"  {command}" for command in commands)]
        raise MigrationError(msg, hints)
    # Removed only now: every refusal above leaves the pristine copy for the rerun.
    remove_scratch(dst)
    if not zfs.unmount(staged):
        msg = f"cannot unmount {staged}; something still uses {dst}"
        raise MigrationError(msg)
    guard.mounted = [name for name in guard.mounted if name != staged]
    # Read before the swap, which sets canmount=noauto on the source. The final dataset gets the
    # source's mountpoint, so the source's effective one says whether it will mount.
    fix = fixups(zfs.props(dataset))
    mount = fix.canmount == "on" and zfs.get("mountpoint", dataset) not in NO_MOUNTPOINT
    markers = (MARK_SOURCE, MARK_DIRS, MARK_KEY)
    # Hinted before the swap: each acts on <dataset> and is idempotent, so it is right whatever step failed.
    guard.cleanup += [
        # mountpoint, sharenfs and sharesmb values can hold spaces.
        *(f"  zfs set {shlex.quote(f'{prop}={value}')} {dataset}" for prop, value in fix.set.items()),
        *(f"  zfs inherit {prop} {dataset}" for prop in (*fix.inherit, *markers)),
        f"  zfs set canmount={fix.canmount} {dataset}",
        *([f"  zfs mount {dataset}"] if mount else []),
    ]
    ensure_container(zfs, f"{settings.stage}/old")
    # canmount=noauto on the staged dataset keeps the swap from mounting it; the fix-ups wait until it is
    # in place, so a stopped swap never leaves the copy with the source's mountpoint.
    swap_into_place(zfs, settings, dataset, staged, guard)
    for prop, value in fix.set.items():
        zfs.set_prop(dataset, prop, value)
    for prop in (*fix.inherit, *markers):
        zfs.inherit(dataset, prop)
    zfs.set_prop(dataset, "canmount", fix.canmount)
    if mount:
        zfs.mount(dataset)
    return [
        f"verify the key with: zfs load-key -n {dataset}",
        "remount NFS clients: the new dataset has new file handles",
        f"re-enable snapshot tools for {dataset}; replication restarts with a full send",
        f"file-move done; once verified, free the old copy with: zfs destroy -r {settings.stage}/old/{leaf}",
    ]


def file_move(
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    *,
    batch_size: int,
    delete_source: bool,
    mount_root: Path = STAGING_MOUNT_ROOT,
    lock_path: Path = LOCK_FILE,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Move the files of ``dataset`` into an encrypted copy, then swap it into place. Rerun to continue."""
    if not delete_source:
        msg = "file-move deletes each source file once its encrypted copy is verified; pass --delete-source to confirm"
        raise MigrationError(msg)
    staged = f"{settings.stage}/{leaf_of(dataset)}"
    with run_lock(lock_path):
        src = preflight_file_move(zfs, settings, dataset)
        source_props = probe_writers(zfs, dataset)
        with abort_guard(zfs, settings, staged, label="file-move") as guard:
            guard.notes = [
                f"file-move stopped; the files are split between {dataset} and {staged}; do not destroy either.",
                "Rerun the same command to continue.",
            ]
            dst, device = open_staged(zfs, settings, dataset, source_props, mount_root, guard)
            # Copying into or over the source would verify each file against itself and delete it.
            if dst.is_relative_to(src) or src.is_relative_to(dst):
                msg = f"{staged} is mounted at {dst}, which overlaps {dataset} at {src}"
                raise MigrationError(msg)
            copy_tree(zfs, staged, src, dst)
            check_passphrase(zfs, settings, staged)
            move_files(zfs, settings, dataset, src, dst, batch_size, sleep, device=device)
            return finish(zfs, settings, dataset, src, dst, guard)
