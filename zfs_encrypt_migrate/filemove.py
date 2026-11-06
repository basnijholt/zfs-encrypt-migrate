"""Encrypt a dataset bigger than the pool's free space by moving its files into an encrypted copy."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import (
    LOCK_FILE,
    MARK_ADMITTED,
    MARK_DIRS,
    MARK_KEY,
    MARK_SOURCE,
    SCRATCH,
    SPACE_ATTEMPTS,
    SPACE_DELAY_SECONDS,
    STAGING_MOUNT_ROOT,
)
from .errors import Interrupted, MigrationError, ZfsError
from .filetree import (
    delete,
    device_of,
    group_by_inode,
    mount_device,
    mounted_at,
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
from .preflight import PROC, holders, pinned_files, quota_notes
from .props import create_props, fixups
from .sizes import format_duration, format_size

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Mapping, Sequence

    from .filetree import Group
    from .migrate import AbortGuard, Settings
    from .zfs import Prop, Zfs

_NO_MOUNTPOINT = {"none", "legacy"}


@contextlib.contextmanager
def _run_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive non-blocking lock so two file-moves never run at once."""
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError(_running(path)) from None
        yield


def _running(path: Path) -> str:
    return f"another file-move is running (lock {path})"


def _refuse_mounted_elsewhere(path: Path, dataset: str) -> None:
    """Refuse unless ``dataset`` itself is the zfs filesystem mounted at ``path``.

    zfs get strips the spaces around a mountpoint value, so the path it gives can be another directory.
    """
    found = mounted_at(path)
    if found != (dataset, "zfs"):
        there = f"{found[0]} of type {found[1]} is mounted there" if found else ""
        msg = f"{path} is not where {dataset} is mounted ({there or 'nothing is mounted there'})"
        raise MigrationError(msg)


def _caught(check: Callable[[], object]) -> MigrationError | None:
    """Return the refusal ``check`` raises, or None; a failed zfs command is no refusal, so it propagates."""
    try:
        check()
    except MigrationError as exc:
        if isinstance(exc, ZfsError):
            raise
        return exc
    return None


def _hinted(problem: MigrationError, *hints: str) -> MigrationError:
    """Add the hints that say how to fix ``problem``."""
    problem.hints = (*problem.hints, *hints)
    return problem


def _movable_problems(zfs: Zfs, dataset: str) -> Generator[MigrationError, None, bool]:
    """Yield why ``dataset`` cannot be file-moved at all; return whether it can."""
    if not zfs.exists(dataset):
        yield MigrationError(f"dataset {dataset} does not exist", ["check the name with: zfs list"])
        return False
    if zfs.get("type", dataset) == "volume":
        yield MigrationError(
            f"{dataset} is a volume; file-move moves the files of a filesystem",
            ["use seed and cutover"],
        )
        return False
    if zfs.get("encryption", dataset) != "off":
        yield MigrationError(f"{dataset} is already encrypted", ["nothing to do"])
        return False
    return True


def _source_problems(zfs: Zfs, dataset: str) -> Generator[MigrationError, None, Path | None]:
    """Yield what makes the source unfit for file-move; return its mountpoint once it is safe to read there."""
    if zfs.children(dataset):
        yield MigrationError(f"{dataset} has child datasets; use the tree commands, or file-move each child")
    if zfs.get("readonly", dataset) == "on":
        yield MigrationError(
            f"{dataset} is read-only; file-move must delete its files",
            [f"zfs set readonly=off {dataset}"],
        )
    mountpoint = zfs.get("mountpoint", dataset)
    if mountpoint in _NO_MOUNTPOINT:
        yield MigrationError(
            f"{dataset} has mountpoint {mountpoint}; file-move needs a mounted path",
            [f"give it a mountpoint: zfs set mountpoint=PATH {dataset}"],
        )
        return None
    if zfs.get("mounted", dataset) != "yes":
        yield MigrationError(f"{dataset} is not mounted", [f"zfs mount {dataset}"])
        return None
    path = Path(mountpoint)
    # Before anything reads the path: it may be another directory, or none at all.
    if problem := _caught(lambda: _refuse_mounted_elsewhere(path, dataset)):
        yield _hinted(
            problem,
            f"findmnt --source {dataset} shows where it is mounted; give it a mountpoint without leading or "
            "trailing spaces",
        )
        return None
    reserved = path / SCRATCH
    # The staged copy keeps its scratch files under this name, so the source must not use it, not even as a
    # dangling symlink.
    if os.path.lexists(reserved):
        yield MigrationError(f"{reserved} exists; file-move needs that name for its own files", ["rename it"])
    return path


def _keys_inside_problems(settings: Settings, dataset: str, source: Path) -> Iterator[MigrationError]:
    """Yield each key file inside the source: a batch would move it into the dataset that needs it to unlock."""
    keys = [("key file", settings.key_file)]
    scheme, _, path = settings.final_keylocation.partition("://")
    if scheme == "file":
        keys.append(("final key file", Path(path)))
    root = source.resolve()
    device = device_of(source)
    for label, key in keys:
        # Symlinks are followed: the file a batch deletes is the target. The device also catches a path outside
        # the mountpoint that reaches the dataset, through a bind mount; a file not there yet has none.
        on_device = device is not None and device_of(key) == device
        if key.resolve().is_relative_to(root) or on_device:
            yield MigrationError(
                f"{label} {key} is inside {dataset}, which file-move empties; keep the key outside the dataset",
            )


def _pinned_space_problems(zfs: Zfs, settings: Settings, dataset: str) -> Iterator[MigrationError]:
    """Yield what keeps deleted files from freeing their space: the move would stall half-way."""
    parts = dataset.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        ancestor = "/".join(parts[:depth])
        # From here up the ancestor holds the stage too, so the copies use what the deletes free in it.
        if settings.stage.startswith(f"{ancestor}/"):
            break
        # A refreservation covers only the ancestor's own data, not what its descendants free.
        value = zfs.get("reservation", ancestor)
        if value not in {"none", "0"}:
            yield MigrationError(
                f"{ancestor} has reservation={value}, which keeps space freed in {dataset} reserved; clear it with: "
                f"zfs set reservation=none {ancestor} (set it again after the move)",
            )
    for prop in ("reservation", "refreservation"):
        value = zfs.get(prop, dataset)
        if value not in {"none", "0"}:
            yield MigrationError(
                f"{dataset} has {prop}={value}, which keeps deleted space reserved; clear it with: "
                f"zfs set {prop}=none {dataset} (set it again on the new dataset after the move)",
            )
    origin = zfs.get("origin", dataset)
    if origin != "-":
        yield MigrationError(
            f"{dataset} is a clone of {origin}; its origin keeps the space of deleted files; "
            f"promote it first: zfs promote {dataset}",
        )
    pool = pool_of(dataset)
    if zfs.pool_get("checkpoint", pool) != "-":
        yield MigrationError(
            f"pool {pool} has a checkpoint, which keeps deleted space allocated; "
            f"discard it first: zpool checkpoint -d {pool}",
        )


def _pool_number(zfs: Zfs, pool: str, prop: str) -> int | None:
    """Read a numeric pool property; None when this OpenZFS does not have it, 0 when it reads ``-``."""
    try:
        value = zfs.pool_get(prop, pool)
    except ZfsError as exc:
        if "invalid property" in exc.result.stderr:
            return None
        raise
    return int(value) if value.isdigit() else 0


def _dedup_problems(zfs: Zfs, dataset: str) -> Iterator[MigrationError]:
    """Yield deduplicated data: deleting a file frees nothing while another reference to its blocks remains."""
    value = zfs.get("dedup", dataset)
    if value != "off":
        yield MigrationError(
            f"{dataset} has dedup={value}; deleting deduplicated files frees no space, so file-move cannot run; "
            "see the docs",
        )
    pool = pool_of(dataset)
    # Blocks written while dedup was on stay shared after it is off. dedupratio rounds a little deduplication
    # down to 1.00x, so the dedup table's size (OpenZFS 2.3 and later) says it first.
    table = _pool_number(zfs, pool, "dedup_table_size")
    if table:
        yield MigrationError(
            f"pool {pool} has a dedup table ({format_size(table)}); deduplicated blocks shared with {dataset} would "
            "not be freed, so file-move cannot run; see the docs",
        )
    if table is not None:
        return
    # zpool get -p prints the ratio without its x.
    ratio = zfs.pool_get("dedupratio", pool).removesuffix("x")
    if float(ratio) > 1:
        yield MigrationError(
            f"pool {pool} has dedupratio {ratio}x; deduplicated blocks shared with {dataset} would not be freed, "
            "so file-move cannot run; see the docs",
        )


def _other_stage_problems(zfs: Zfs, dataset: str) -> Iterator[MigrationError]:
    """Yield, on a first run, each other stage that holds a file-move of ``dataset``.

    Its files are split between the source and that staged dataset; a second staged dataset would get only
    the rest, and its swap would finish with half the files.
    """
    for name, value in zfs.local_values(MARK_SOURCE, pool_of(dataset)):
        if value == dataset:
            yield MigrationError(
                f"{name} already holds a file-move of {dataset}; rerun with --stage {name.rsplit('/', 1)[0]}",
            )


def _snapshot_problems(zfs: Zfs, dataset: str, source: Path) -> Iterator[MigrationError]:
    """Yield the snapshots of the source while files are left to move, which they would keep allocated.

    Here as well as before each batch, so a snapshot stops the run before anything is written. Only while files are
    left: then a snapshot pins nothing, and a rerun after a refused swap keeps its renamed enc-final.
    """
    if not zfs.snapshots(dataset):
        return
    try:
        left = scan(source)
    except MigrationError as exc:
        yield exc
        return
    if left and (problem := _caught(lambda: _refuse_snapshots(zfs, dataset))):
        yield problem


def _stage_problems(zfs: Zfs, settings: Settings, dataset: str, source: Path | None) -> Iterator[MigrationError]:
    """Yield what about the stage, or the state of this move, keeps the run from starting or resuming."""
    stage = settings.stage
    staged = f"{stage}/{leaf_of(dataset)}"
    staged_exists = zfs.exists(staged)
    # Checked once, before anything is created or deleted: a later run sees the space the earlier ones used.
    if not staged_exists:
        yield from _dedup_problems(zfs, dataset)
        yield from _other_stage_problems(zfs, dataset)
    if source is not None:
        yield from _snapshot_problems(zfs, dataset, source)
    if staged_exists and zfs.get(MARK_SOURCE, staged) != dataset:
        yield MigrationError(
            f"{staged} exists and is not a file-move of {dataset}",
            ["finish or clear the migration that owns it first"],
        )
    if [name for name in in_flight(zfs, settings) if name != staged]:
        yield MigrationError(
            f"a migration is already in flight under {stage}",
            [f"finish the migration under {stage} first"],
        )
    old = f"{stage}/old/{leaf_of(dataset)}"
    if not staged_exists and zfs.exists(old):
        yield MigrationError(
            f"{old} already exists",
            [f"destroy it once you no longer need it (zfs destroy -r {old}), or rename it"],
        )


def _preflight_problems(zfs: Zfs, settings: Settings, dataset: str) -> Generator[MigrationError, None, Path | None]:
    """Yield every reason the run cannot start or resume, in the order the run refuses them. Writes nothing.

    Return the source mountpoint, or None when nothing there may be read: the dataset cannot be moved at all, or the
    path is not its own mount. A check that means nothing once an earlier one failed is skipped.
    """
    key = settings.key_file
    if problem := _caught(lambda: check_key_file(settings)):
        yield _hinted(problem, f"chmod 600 {key}" if key.is_file() else "write the passphrase into it, with mode 600")
    if "/" not in dataset:
        yield MigrationError(
            f"{dataset} is a pool root; file-move is not supported",
            ["a pool root cannot be swapped; see Rebuilding a Whole Pool Encrypted in the guides"],
        )
        return None
    if problem := _caught(lambda: refuse_cross_pool(settings, dataset, "file-move")):
        yield _hinted(problem, f"use a stage on pool {pool_of(dataset)}")
    if problem := _caught(lambda: refuse_nested(settings, dataset)):
        yield _hinted(problem, f"use a stage that does not overlap {dataset}")
    if not (yield from _movable_problems(zfs, dataset)):
        return None
    source = yield from _source_problems(zfs, dataset)
    if source is not None:
        yield from _keys_inside_problems(settings, dataset, source)
    # zfs get mountpoint includes the altroot, so the swap's fix-up would store it in the mountpoint.
    pool = pool_of(dataset)
    altroot = zfs.pool_get("altroot", pool)
    if altroot != "-":
        yield MigrationError(
            f"pool {pool} is imported with altroot={altroot}; file-move needs a normal import",
            [f"export the pool and import it without -R: zpool export {pool}, then zpool import {pool}"],
        )
    yield from _pinned_space_problems(zfs, settings, dataset)
    yield from _stage_problems(zfs, settings, dataset, source)
    return source


def _preflight_file_move(zfs: Zfs, settings: Settings, dataset: str) -> Path:
    """Refuse unless the run can start or resume; return the source mountpoint. Writes nothing."""
    problems = _preflight_problems(zfs, settings, dataset)
    try:
        problem = next(problems)
    except StopIteration as done:
        source: Path = done.value
        return source
    raise problem


def _probe_writers(zfs: Zfs, dataset: str, path: Path) -> dict[str, Prop]:
    """Unmount the source and mount it again with noatime: a busy dataset means a writer is still running.

    Return the source's properties, read while it is unmounted: once mounted with noatime, zfs reports
    atime as a temporary value and hides the dataset's own. Refuse unless the dataset is then what is
    mounted at ``path``, the mountpoint file-move works on.
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
    _refuse_mounted_elsewhere(path, dataset)
    return props


def _open_staged(  # noqa: PLR0917
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    source_props: Mapping[str, Prop],
    mount_root: Path,
    guard: AbortGuard,
) -> tuple[Path, int]:
    """Create the encrypted staged dataset on the first run; load its key and mount it.

    Refuse unless it is then the filesystem mounted at its mountpoint. Return that mountpoint and the device of
    the mounted dataset, which every later copy must be on.
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
    _refuse_mounted_elsewhere(path, staged)
    return path, device


# rsync compares times in whole seconds by default; a negative window (rsync 3.1.3+) adds the nanoseconds.
# Without it, a copy whose time is off by less than a second is skipped by the quick check, then refused by
# verify on every rerun, and the pristine pass cannot restore a directory time off by less than a second.
_MODIFY_WINDOW = "--modify-window=-1"


def _rsync(zfs: Zfs, args: list[str]) -> None:
    result = zfs.runner.run(args)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        msg = f"{shlex.join(args)} failed: {detail}"
        raise MigrationError(msg)


def _rsync_files(zfs: Zfs, src: Path, dst: Path, file_list: Path) -> None:
    """Copy the NUL-separated list of files, keeping hardlinks, ACLs, xattrs and sparseness."""
    temp = temp_dir(dst)
    temp.mkdir(parents=True, exist_ok=True)
    _rsync(
        zfs,
        [
            "rsync",
            "-aHAXS",
            "--numeric-ids",
            _MODIFY_WINDOW,
            "--from0",
            f"--files-from={file_list}",
            f"--temp-dir={temp}",
            f"{src}/",
            f"{dst}/",
        ],
    )


def _rsync_dirs(zfs: Zfs, src: Path, dst: Path) -> None:
    """Copy only the directory tree with its metadata."""
    _rsync(
        zfs,
        [
            "rsync",
            "-aAX",
            "--numeric-ids",
            _MODIFY_WINDOW,
            "--exclude=/.zfs",
            "--include=*/",
            "--exclude=*",
            f"{src}/",
            f"{dst}/",
        ],
    )


def _copy_tree(zfs: Zfs, staged: str, src: Path, dst: Path, device: int) -> None:
    """Copy the directories twice, the second time into scratch so later batches can restore their mtimes."""
    if zfs.get(MARK_DIRS, staged) == "copied":
        return
    _rsync_dirs(zfs, src, dst)
    shadow = dst / SCRATCH / "dirs"
    shadow.mkdir(parents=True, exist_ok=True)
    _rsync_dirs(zfs, src, shadow)
    # The marker vouches for copies in the staged dataset; after an unmount they went to the bare directory.
    _refuse_unmounted(zfs, staged, dst, device)
    zfs.set_prop(staged, MARK_DIRS, "copied")


def _stdin_is_terminal() -> bool:
    """Return whether a person can type a passphrase."""
    return sys.stdin.isatty()


def _raw_key(zfs: Zfs, settings: Settings, staged: str) -> bool:
    """Return whether the staged key is raw, which zfs refuses to take typed at a terminal.

    The staged dataset's own keyformat counts: a rerun may leave out the --keyformat raw that created it.
    """
    return settings.keyformat == "raw" or zfs.get("keyformat", staged) == "raw"


def _check_passphrase(zfs: Zfs, settings: Settings, staged: str, *, check_prompt_with_key_file: bool = False) -> None:
    """Prove the final key unlocks the staged dataset before any file is deleted.

    The marker records the location that was checked, so a rerun with another final location checks again. Only
    when the operator vouches for the key file is a final ``prompt`` checked against that file, on every run, never
    because no terminal is attached. That proves the file opens the dataset, not that anyone knows the passphrase.
    """
    location = settings.final_keylocation
    from_key_file = location == "prompt" and check_prompt_with_key_file
    if from_key_file and _raw_key(zfs, settings, staged):
        msg = (
            f"{staged} has keyformat=raw, which can't be typed at the prompt; "
            "--check-prompt-with-key-file needs a passphrase or hex key"
        )
        raise MigrationError(msg)
    check = f"file://{settings.key_file}" if from_key_file else location
    checked = zfs.get(MARK_KEY, staged)
    # The key file is checked on every run: it may have changed since it passed, and the staged key a killed run
    # left loaded would not notice before files are deleted.
    if checked == location and not from_key_file:
        return
    if check == "prompt":
        if not _stdin_is_terminal():
            msg = "run file-move once from a terminal so it can check the passphrase"
            hint = (
                "or, if the key file holds the passphrase you keep, check that file instead: "
                "rerun with --check-prompt-with-key-file"
            )
            # The flag refuses a raw key, so it is no way out for one.
            raise MigrationError(msg, [] if _raw_key(zfs, settings, staged) else [hint])
        loaded = zfs.check_key(staged, "prompt", attached=True)
    else:
        loaded = zfs.check_key(staged, check, attached=False)
    if not loaded:
        msg = f"the key for {staged} did not load from {check}; nothing was deleted"
        raise MigrationError(msg)
    # A passphrase typed at a terminal stays the record: a later run without the flag relies on it.
    if checked != check and not (from_key_file and checked == "prompt"):
        zfs.set_prop(staged, MARK_KEY, check)


def _wait_for_space(zfs: Zfs, settings: Settings, needed: int, sleep: Callable[[float], None]) -> None:
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


def _admission_shortfall(
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    groups: Sequence[Group],
    batch_size: int,
) -> str | None:
    """Return why free space does not cover the worst case of the move, or None when it does. Reads only.

    The worst case adds up:

    - the largest batch, which never holds more than all the files, or the largest file when that is bigger:
      moving the other files first frees only what their copies take, so it never gets more room than it has
      now;
    - the pool's block-cloned savings (``bclonesaved``, before OpenZFS 2.2 none): deleting one reference to a
      cloned block frees nothing while another remains, and the copy takes the full size;
    - with compression now off, what compressed data grows by when copied: logicalreferenced - referenced.
    """
    pool = pool_of(dataset)
    total = sum(group.allocated for group in groups)
    largest = max(min(batch_size, total), *(group.allocated for group in groups))
    saved = _pool_number(zfs, pool, "bclonesaved") or 0
    loss = 0
    if zfs.get("compression", dataset) == "off":
        loss = max(int(zfs.get("logicalreferenced", dataset)) - int(zfs.get("referenced", dataset)), 0)
    need = largest + saved + loss
    budget = zfs.pool_avail(pool) - settings.min_free
    if budget >= need:
        return None
    return (
        f"free space on {pool} above the floor ({format_size(max(budget, 0))}) does not cover the worst case of "
        f"this move ({format_size(need)}: largest batch or file {format_size(largest)}, block-cloned data "
        f"{format_size(saved)}, lost compression {format_size(loss)})"
    )


def _admit(
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    groups: Sequence[Group],
    batch_size: int,
) -> None:
    """Refuse, before the move copies or deletes any file, unless free space covers its worst case; mark it admitted.

    Checked once per move: copies a stopped run left behind use the space counted here, so a rerun after the
    marker is set does not check again.
    """
    staged = f"{settings.stage}/{leaf_of(dataset)}"
    if zfs.get(MARK_ADMITTED, staged) == "yes":
        return
    shortfall = _admission_shortfall(zfs, settings, dataset, groups, batch_size)
    if shortfall is not None:
        hint = f"nothing was deleted; to give up, remove the staged dataset: zfs destroy -r {staged}"
        raise MigrationError(shortfall, [hint])
    zfs.set_prop(staged, MARK_ADMITTED, "yes")


def _refuse_unmounted(zfs: Zfs, staged: str, dst: Path, device: int) -> None:
    """Refuse unless the staged dataset is still the filesystem mounted at ``dst``.

    Once it is unmounted, ``dst`` is a bare directory: copies there pass verify, and the source files deleted
    against them are gone once the dataset mounts over them again or the host reboots.
    """
    if zfs.get("mounted", staged) != "yes" or mount_device(dst) != device:
        msg = f"{staged} is no longer mounted at {dst}; nothing of this batch was deleted"
        raise MigrationError(msg)


def _split_notes(dataset: str, staged: str) -> list[str]:
    """Return the guard's notes for a stop once files were deleted: they are split between the two datasets."""
    return [
        f"file-move stopped; the files are split between {dataset} and {staged}; do not destroy either.",
        "Rerun the same command to continue.",
    ]


def _batch_line(
    *,
    number: int,
    files: int,
    moved: int,
    moved_data: int,
    copied: int,
    seconds: float,
    left: int,
    left_data: int,
    run_copied: int,
    run_seconds: float,
    stamp: float,
) -> str:
    """Format the progress line of a finished batch; it always starts with ``batch N:``.

    ``files``, ``moved`` and ``moved_data`` (allocated and apparent bytes) count what the batch deleted, and
    ``left`` and ``left_data`` what is still in the source. ``copied`` is what the batch wrote into the copy
    in ``seconds``, and ``run_copied`` in ``run_seconds`` is the same for every batch of this run so far: the rate
    and the ETA come from those, since a copy that already existed took no time. With no elapsed time there is
    no rate, and with nothing copied or nothing left there is no ETA. ``stamp`` is the epoch time of the line.
    """
    text = f"batch {number}: {files} files, {format_size(moved)} moved ({format_size(moved_data)} data)"
    text += f" in {format_duration(seconds)}"
    if seconds > 0:
        text += f" at {format_size(int(copied / seconds))}/s"
    text += f", {format_size(left)} left ({format_size(left_data)} data)"
    if run_seconds > 0 and run_copied > 0 and left > 0:
        text += f", ETA {format_duration(left / (run_copied / run_seconds))}"
    return f"{text} [{datetime.fromtimestamp(stamp).strftime('%H:%M')}]"  # noqa: DTZ006


def _move_files(  # noqa: PLR0917
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    src: Path,
    dst: Path,
    batch_size: int,
    sleep: Callable[[float], None],
    *,
    device: int,
    guard: AbortGuard,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
) -> None:
    """Move every file into the copy in verified batches; delete a batch only after its copy is verified and synced.

    ``clock`` is a monotonic clock that times each batch; ``wall_clock`` returns the epoch time that stamps its line.

    ``device`` is the device of the staged dataset mounted at ``dst``; every copy must be on it. From the first
    delete on, ``guard`` says the files are split.
    """
    pool = pool_of(settings.stage)
    staged = f"{settings.stage}/{leaf_of(dataset)}"
    scratch = dst / SCRATCH
    # Before any space check, since they hold space; the run lock keeps every other run out of scratch.
    remove_partials(dst)
    batch_number = 0
    # Of the batches finished in this run only, so the ETA does not depend on earlier runs.
    run_copied = 0
    run_seconds = 0.0
    while entries := scan(src):
        groups = group_by_inode(entries, src)
        # Before the first batch of the move; skipped once the staged dataset is marked admitted.
        _admit(zfs, settings, dataset, groups, batch_size)
        left = sum(group.allocated for group in groups)
        left_data = sum(group.apparent for group in groups)
        deleted = 0
        for batch in plan_batches(groups, batch_size):
            started = clock()
            # Before anything is written into dst: it may have become a bare directory since the last batch.
            _refuse_unmounted(zfs, staged, dst, device)
            # Created only when files remain: a rerun after finish removed scratch must not change the copy's root.
            scratch.mkdir(exist_ok=True)
            # Checked before every batch: a snapshot that appears mid-run would pin the deleted files.
            _refuse_snapshots(zfs, dataset)
            # A stop between copy and delete leaves copies that need no new space; rsync skips them.
            missing = sum(group.allocated for group in batch if not os.path.lexists(dst / group.first))
            _wait_for_space(zfs, settings, missing, sleep)
            write_file_list(scratch / "files", batch)
            _rsync_files(zfs, src, dst, scratch / "files")
            verified, _changed = verify(src, dst, batch, device=device)
            zfs.pool_sync(pool)
            # And again right before deleting: a snapshot taken while the batch copied would pin it.
            _refuse_snapshots(zfs, dataset)
            if verified:
                # Set before the first unlink, so a stop part-way through it already gives the note.
                guard.notes = _split_notes(dataset, staged)
            deleted += delete(src, verified)
            batch_number += 1
            # Only names that are gone count as moved: a group verification or delete kept comes back next pass.
            gone = [g for g in verified if not any(os.path.lexists(src / item.name) for item in g.entries)]
            moved = sum(group.allocated for group in gone)
            moved_data = sum(group.apparent for group in gone)
            left -= moved
            left_data -= moved_data
            seconds = max(clock() - started, 0.0)
            run_copied += missing
            run_seconds += seconds
            console.print(
                _batch_line(
                    number=batch_number,
                    files=sum(len(group.entries) for group in gone),
                    moved=moved,
                    moved_data=moved_data,
                    copied=missing,
                    seconds=seconds,
                    left=left,
                    left_data=left_data,
                    run_copied=run_copied,
                    run_seconds=run_seconds,
                    stamp=wall_clock(),
                ),
                soft_wrap=True,
            )
        if deleted == 0:
            msg = f"{len(entries)} files kept changing during the move; stop the writers and rerun"
            raise MigrationError(msg)


def _finish(  # noqa: PLR0917
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
    _probe_writers(zfs, dataset, src)
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
        _rsync_dirs(zfs, src, dst)
        _rsync_dirs(zfs, pristine, dst)
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
    mount = fix.canmount == "on" and zfs.get("mountpoint", dataset) not in _NO_MOUNTPOINT
    markers = (MARK_SOURCE, MARK_DIRS, MARK_KEY, MARK_ADMITTED)
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
    check_prompt_with_key_file: bool = False,
    mount_root: Path = STAGING_MOUNT_ROOT,
    lock_path: Path = LOCK_FILE,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Move the files of ``dataset`` into an encrypted copy, then swap it into place. Rerun to continue."""
    if not delete_source:
        msg = "file-move deletes each source file once its encrypted copy is verified; pass --delete-source to confirm"
        raise MigrationError(msg)
    staged = f"{settings.stage}/{leaf_of(dataset)}"
    with _run_lock(lock_path):
        src = _preflight_file_move(zfs, settings, dataset)
        source_props = _probe_writers(zfs, dataset, src)
        with abort_guard(zfs, settings, staged, label="file-move") as guard:
            # Until the first delete every file is still in the source. Files only ever get deleted once the
            # move is admitted, so an earlier run of an admitted move may have split them already.
            if zfs.exists(staged) and zfs.get(MARK_ADMITTED, staged) == "yes":
                guard.notes = _split_notes(dataset, staged)
            dst, device = _open_staged(zfs, settings, dataset, source_props, mount_root, guard)
            # Copying into or over the source would verify each file against itself and delete it.
            if dst.is_relative_to(src) or src.is_relative_to(dst):
                msg = f"{staged} is mounted at {dst}, which overlaps {dataset} at {src}"
                raise MigrationError(msg)
            _copy_tree(zfs, staged, src, dst, device)
            _check_passphrase(zfs, settings, staged, check_prompt_with_key_file=check_prompt_with_key_file)
            _move_files(
                zfs,
                settings,
                dataset,
                src,
                dst,
                batch_size,
                sleep,
                device=device,
                guard=guard,
            )
            return _finish(zfs, settings, dataset, src, dst, guard)


@dataclass(frozen=True)
class CheckReport:
    """What ``file-move --check`` found: problems that would make the run refuse or stall, and notes that would not."""

    problems: list[MigrationError]
    notes: list[MigrationError]


@contextlib.contextmanager
def _lock_shared(path: Path) -> Iterator[bool]:
    """Share the run lock while the check reads, so no run starts meanwhile; yield whether a run holds it.

    Never creates the lock file: without one, no run has started since boot.
    """
    if not path.exists():
        yield False
        return
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            running = True
        else:
            running = False
        yield running


def _gather(problems: Generator[MigrationError, None, Path | None], found: list[MigrationError]) -> Path | None:
    """Add every one of ``problems`` to ``found``; return the source mountpoint they end with."""
    while True:
        try:
            found.append(next(problems))
        except StopIteration as done:
            source: Path | None = done.value
            return source


def _admission_problems(
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    source: Path,
    batch_size: int,
) -> list[MigrationError]:
    """Return the refusal of the free-space admission, which the run reaches only after it created the staged copy."""
    staged = f"{settings.stage}/{leaf_of(dataset)}"
    if zfs.exists(staged) and zfs.get(MARK_ADMITTED, staged) == "yes":
        return []
    try:
        groups = group_by_inode(scan(source), source)
    except MigrationError as exc:
        return [exc]
    shortfall = _admission_shortfall(zfs, settings, dataset, groups, batch_size) if groups else None
    if shortfall is None:
        return []
    pool = pool_of(dataset)
    hint = (
        f"free space on {pool}, lower --batch-size if the largest batch is the biggest term, or remove clones you no "
        f"longer need (zpool get bcloneused,bclonesaved {pool})"
    )
    return [MigrationError(shortfall, [hint])]


def check_file_move(
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    *,
    batch_size: int,
    proc: Path = PROC,
    lock_path: Path = LOCK_FILE,
) -> CheckReport:
    """Report everything that would make file-move refuse or stall, all at once, and change nothing.

    It runs the run's own checks without stopping at the first, then what the run finds only once it is under way:
    the free-space admission, files it cannot delete, and whatever keeps the dataset in use or mounted, in any mount
    namespace (read from ``proc``). It does not unmount the dataset, as the run does to prove that no writer has it
    open. It holds ``lock_path`` shared while it reads, so no run starts meanwhile, and reads nothing while a run
    holds it.
    """
    with _lock_shared(lock_path) as running:
        if running:
            return CheckReport([MigrationError(_running(lock_path), ["check again once it has stopped"])], [])
        problems: list[MigrationError] = []
        notes: list[MigrationError] = []
        source = _gather(_preflight_problems(zfs, settings, dataset), problems)
        if source is not None:
            problems += _admission_problems(zfs, settings, dataset, source, batch_size)
            problems += pinned_files(source, dataset)
            problems += holders(proc, dataset, source)
            notes += quota_notes(zfs, dataset, source)
    # The snapshot check and the admission both scan, so a refusal of the scan comes up twice.
    unique = {problem.message: problem for problem in problems}
    return CheckReport(list(unique.values()), notes)
