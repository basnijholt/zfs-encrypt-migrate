"""Scan, batch, verify and delete the files of a dataset moved batch by batch."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import SCRATCH
from .errors import MigrationError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

_BLOCK = 512


@dataclass(frozen=True)
class Entry:
    """One non-directory name under the source root."""

    name: str
    ino: int
    nlink: int
    ctime_ns: int
    allocated: int


@dataclass(frozen=True)
class Group:
    """All names of one inode; a group always moves whole."""

    entries: tuple[Entry, ...]

    @property
    def first(self) -> str:
        """Return the first name (entries are sorted by name)."""
        return self.entries[0].name

    @property
    def allocated(self) -> int:
        """Return the allocated bytes of the inode, counted once."""
        return self.entries[0].allocated


def scan(root: Path) -> list[Entry]:
    """List every non-directory under ``root`` without following symlinks."""
    root_dev = os.lstat(root).st_dev
    entries: list[Entry] = []
    pending = [(str(root), "")]
    while pending:
        path, prefix = pending.pop()
        try:
            with os.scandir(path) as it:
                children = list(it)
        except FileNotFoundError:
            continue
        for child in children:
            name = prefix + child.name
            if not prefix and child.name == ".zfs":
                continue
            try:
                if child.is_dir(follow_symlinks=False):
                    if os.lstat(child.path).st_dev != root_dev:
                        msg = f"{child.path} is another filesystem mounted inside {root}"
                        raise MigrationError(msg)
                    pending.append((child.path, name + os.sep))
                    continue
                st = child.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            entries.append(Entry(name, st.st_ino, st.st_nlink, st.st_ctime_ns, st.st_blocks * _BLOCK))
    return entries


def group_by_inode(entries: Iterable[Entry], root: Path) -> list[Group]:
    """Join the names of each inode; refuse when a hard link lives outside ``root``."""
    by_ino: dict[int, list[Entry]] = {}
    for item in entries:
        by_ino.setdefault(item.ino, []).append(item)
    groups = [Group(tuple(sorted(found, key=lambda e: e.name))) for found in by_ino.values()]
    groups.sort(key=lambda g: g.first)
    for g in groups:
        nlink = g.entries[0].nlink
        if nlink != len(g.entries):
            msg = f"{root / g.first} has {nlink} hard links but only {len(g.entries)} were found under {root}"
            raise MigrationError(
                msg,
            )
    return groups


def plan_batches(groups: Sequence[Group], batch_size: int) -> list[list[Group]]:
    """Split groups in order into batches of at most ``batch_size`` allocated bytes."""
    batches: list[list[Group]] = []
    current: list[Group] = []
    used = 0
    for g in groups:
        if current and used + g.allocated > batch_size:
            batches.append(current)
            current, used = [], 0
        current.append(g)
        used += g.allocated
    if current:
        batches.append(current)
    return batches


def write_file_list(path: Path, groups: Sequence[Group]) -> None:
    """Write every name NUL-terminated, for ``rsync --from0 --files-from``.

    rsync reads a listed name that starts with ``#`` or ``;`` as a comment, even with ``--from0``, and
    skips it without an error; the ``./`` prefix keeps every name a path.
    """
    with path.open("wb") as f:
        for g in groups:
            for item in g.entries:
                f.write(b"./" + os.fsencode(item.name) + b"\0")


def _unchanged(src: Path, g: Group, *, missing_ok: bool = False) -> bool:
    for item in g.entries:
        try:
            st = os.lstat(src / item.name)
        except FileNotFoundError:
            if missing_ok:
                continue
            return False
        if st.st_ino != item.ino or st.st_ctime_ns != item.ctime_ns:
            return False
    return True


def _check_copy(src: Path, dst: Path, name: str) -> int:
    """Raise unless ``dst/name`` matches ``src/name``; return the copy's inode."""
    s = os.lstat(src / name)
    try:
        d = os.lstat(dst / name)
    except FileNotFoundError:
        msg = f"{name} is missing from the copy"
        raise MigrationError(msg) from None
    checks: list[tuple[str, object, object]] = [
        ("type", stat.S_IFMT(s.st_mode), stat.S_IFMT(d.st_mode)),
        ("mode", stat.S_IMODE(s.st_mode), stat.S_IMODE(d.st_mode)),
        ("uid", s.st_uid, d.st_uid),
        ("gid", s.st_gid, d.st_gid),
    ]
    if stat.S_ISREG(s.st_mode):
        checks += [("size", s.st_size, d.st_size), ("mtime", s.st_mtime_ns, d.st_mtime_ns)]
    elif stat.S_ISLNK(s.st_mode):
        checks.append(("target", (src / name).readlink(), (dst / name).readlink() if stat.S_ISLNK(d.st_mode) else None))
    elif stat.S_ISCHR(s.st_mode) or stat.S_ISBLK(s.st_mode):
        checks.append(("rdev", s.st_rdev, d.st_rdev))
    for field, want, got in checks:
        if want != got:
            msg = f"{name} differs from its copy ({field})"
            raise MigrationError(msg)
    return d.st_ino


def verify(src: Path, dst: Path, groups: Sequence[Group]) -> tuple[list[Group], list[Group]]:
    """Split groups into verified copies and groups whose source changed since the scan.

    Any mismatch between source and copy raises, so nothing unverified is ever deleted.
    """
    verified: list[Group] = []
    changed: list[Group] = []
    for g in groups:
        if not _unchanged(src, g):
            changed.append(g)
            continue
        inodes = {_check_copy(src, dst, item.name) for item in g.entries}
        if len(inodes) != 1:
            msg = f"hard links of {g.first} were not kept in the copy"
            raise MigrationError(msg)
        verified.append(g)
    return verified, changed


def delete(src: Path, groups: Sequence[Group]) -> int:
    """Unlink every name of every unchanged group (never directories); return how many were unlinked.

    A group is re-checked against its scan entries first, because a writer can replace a name
    between verify and delete; unlinking that new file would lose data that was never copied.
    All names are checked before the first unlink, since unlinking one changes the inode's ctime.
    """
    count = 0
    for g in groups:
        if not _unchanged(src, g, missing_ok=True):
            continue
        for item in g.entries:
            try:
                (src / item.name).unlink()
            except FileNotFoundError:
                continue
            count += 1
    return count


def remove_scratch(dst: Path) -> None:
    """Remove whatever is left of the scratch directory; restore the root's times from the pristine copy.

    A run stopped part-way through an earlier removal leaves only part of scratch behind; without the
    pristine copy there are no original times to restore.
    """
    scratch = dst / SCRATCH
    if not os.path.lexists(scratch):
        return
    try:
        st: os.stat_result | None = os.lstat(scratch / "dirs")
    except FileNotFoundError:
        st = None
    shutil.rmtree(scratch)
    if st is not None:
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
