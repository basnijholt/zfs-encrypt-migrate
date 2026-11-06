"""Report where a file-move stands. Strictly read-only: it runs only ``zfs list`` and ``zfs get``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import MARK_ADMITTED, MARK_DIRS, MARK_KEY, MARK_SOURCE
from .migrate import leaf_of

if TYPE_CHECKING:
    from .zfs import Zfs

_PROC_LOCKS = Path("/proc/locks")


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
    except OSError as error:
        return f"unknown ({path} cannot be read: {error.strerror or error})"
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


_PROPS = ("encryption", "keystatus", "mounted", "used", "logicalused", MARK_SOURCE, MARK_KEY, MARK_ADMITTED, MARK_DIRS)


def _facts(zfs: Zfs, name: str) -> list[str]:
    """List the facts of a dataset, markers included, or that it does not exist. One ``zfs get``."""
    if not zfs.exists(name):
        return [f"{name}: does not exist"]
    return [f"{name}: exists", *(f"  {prop}: {value}" for prop, value in zfs.get_many(_PROPS, name))]


def file_move_status(zfs: Zfs, stage: str, dataset: str, lock_path: Path) -> list[str]:
    """Return the facts about the file-move of ``dataset`` through ``stage``; it infers and advises nothing."""
    leaf = leaf_of(dataset)
    return [
        f"file-move status of {dataset} (stage {stage})",
        *_facts(zfs, dataset),
        *_facts(zfs, f"{stage}/{leaf}"),
        f"{stage}/old/{leaf}: {'exists' if zfs.exists(f'{stage}/old/{leaf}') else 'does not exist'}",
        f"lock: {_lock_state(lock_path)}",
    ]
