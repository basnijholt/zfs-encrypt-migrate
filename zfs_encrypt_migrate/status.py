"""Report where a file-move stands. Strictly read-only: it runs only ``zfs list`` and ``zfs get``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import LOCK_FILE, MARK_ADMITTED, MARK_DIRS, MARK_KEY, MARK_SOURCE
from .errors import MigrationError
from .filetree import has_files, mounted_at
from .migrate import leaf_of
from .sizes import format_size

if TYPE_CHECKING:
    from .zfs import Zfs

_PROC_LOCKS = Path("/proc/locks")
_NO_MOUNTPOINT = {"none", "legacy"}


def _read_locks() -> str:
    return _PROC_LOCKS.read_text()


def _lock_state(path: Path) -> str:
    """Say whether a run holds the lock at ``path``, without opening, creating or locking the file.

    ``file-move`` takes a ``flock`` lock. ``fcntl`` F_GETLK cannot see those on Linux, so this finds the file's
    device and inode in ``/proc/locks``, which lists every lock the kernel holds. Only a ``stat`` touches the
    lock file, so a status check can never make a starting run fail or wait. The lock file stays after a run
    ends: its existence says nothing. A waiter (a ``->`` line) is not a holder.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return "no run"
    try:
        text = _read_locks()
    except OSError:
        return f"unknown ({_PROC_LOCKS} cannot be read)"
    for line in text.splitlines():
        fields = line.split()
        if "->" in fields or len(fields) < 6 or fields[1] != "FLOCK":  # noqa: PLR2004
            continue
        try:
            major, minor, inode = fields[5].split(":")
            same = (int(major, 16), int(minor, 16), int(inode)) == (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
        except ValueError:
            continue
        if same:
            return f"held by a running file-move (pid {fields[4]})"
    return "no run holds it"


def _marker(zfs: Zfs, prop: str, name: str, yes: str) -> str:
    return "yes" if zfs.get(prop, name) == yes else "no"


def _sizes(zfs: Zfs, name: str) -> str:
    used, logical = (int(zfs.get(prop, name)) for prop in ("used", "logicalused"))
    return f"  {name}: used {format_size(used)}, logicalused {format_size(logical)}"


def _source_phase(zfs: Zfs, dataset: str) -> tuple[str, str | None]:
    """Return the phase of an admitted move and a note, from what the source still holds."""
    unknown = "moving files or only directories left"
    mountpoint = zfs.get("mountpoint", dataset)
    if mountpoint in _NO_MOUNTPOINT or zfs.get("mounted", dataset) != "yes":
        return unknown, f"{dataset} is not mounted, so its files were not counted"
    if mounted_at(Path(mountpoint)) != (dataset, "zfs"):
        return unknown, f"{dataset} is not what is mounted at {mountpoint}, so its files were not counted"
    try:
        files = has_files(Path(mountpoint))
    except (MigrationError, OSError) as error:
        return unknown, f"files were not counted: {error}"
    return ("moving files", None) if files else ("only directories left", None)


def _after_swap(zfs: Zfs, stage: str, dataset: str) -> tuple[str, list[str]]:
    leaf = leaf_of(dataset)
    if zfs.get(MARK_SOURCE, dataset) not in {"-", ""}:
        markers = (MARK_SOURCE, MARK_DIRS, MARK_KEY, MARK_ADMITTED)
        steps = [f"  zfs inherit {prop} {dataset}" for prop in markers]
        return "swapped, final steps unfinished", [
            "next: the swap happened but its property fix-ups did not finish; follow the hints the run printed, or:",
            *steps,
        ]
    return "swapped", [
        "next: no rerun needed. Finish by hand:",
        f"  zfs load-key -n {dataset}",
        "  remount NFS clients: the new dataset has new file handles",
        f"  re-enable snapshot tools for {dataset}; replication restarts with a full send",
        f"  once verified, free the old copy: zfs destroy -r {stage}/old/{leaf}",
    ]


def file_move_status(zfs: Zfs, stage: str, dataset: str, *, lock_path: Path = LOCK_FILE) -> list[str]:
    """Return the report lines for the file-move of ``dataset`` through ``stage``. Reads only."""
    staged = f"{stage}/{leaf_of(dataset)}"
    lock = _lock_state(lock_path)
    running = lock.startswith("held")
    lines = [f"file-move status of {dataset} (stage {stage})"]
    if not zfs.exists(dataset):
        lines += [
            f"{dataset}: does not exist",
            f"lock: {lock}",
            f"next: if {stage}/old/{leaf_of(dataset)} exists, a swap was interrupted; follow the recovery hints",
        ]
        return lines
    lines.append(f"{dataset}: exists")
    lines.append(_sizes(zfs, dataset))
    note = None
    if not zfs.exists(staged):
        lines.append(f"{staged}: does not exist")
        swapped = zfs.get("encryption", dataset) != "off"
        phase, tail = _after_swap(zfs, stage, dataset) if swapped else ("not started", [])
        steps = [f"next: run file-move --stage {stage} {dataset} --delete-source"]
        tail = tail or steps
    else:
        lines.append(f"{staged}: exists")
        owner = zfs.get(MARK_SOURCE, staged)
        key = zfs.get(MARK_KEY, staged)
        lines += [
            f"  {MARK_SOURCE}: {owner}",
            f"  {MARK_KEY}: {key if key not in {'-', ''} else 'no'}",
            f"  {MARK_ADMITTED}: {_marker(zfs, MARK_ADMITTED, staged, 'yes')}",
            f"  {MARK_DIRS}: {_marker(zfs, MARK_DIRS, staged, 'copied')}",
            _sizes(zfs, staged),
        ]
        if owner != dataset:
            phase = "unknown"
            tail = [f"next: {staged} is not a file-move of {dataset}; another migration owns that name"]
        elif zfs.get(MARK_ADMITTED, staged) == "yes":
            phase, note = _source_phase(zfs, dataset)
            tail = [f"next: rerun the same file-move command to continue; do not destroy either {dataset} or {staged}"]
        else:
            phase = "copying the tree"
            tail = ["next: rerun the same file-move command to continue; nothing was deleted yet"]
    if running and phase not in {"swapped"}:
        tail = ["next: wait; a run holds the lock and a second one would be refused"]
    lines.append(f"phase: {phase}")
    if note:
        lines.append(f"  note: {note}")
    lines.append(f"lock: {lock}")
    lines += tail
    return lines
