"""Scan, batch, verify and delete the files of a dataset moved batch by batch."""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import SCRATCH
from .errors import MigrationError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_BLOCK = 512
# An encrypted dataset cannot embed a tiny file's data in its block pointer, so the copy takes a whole sector.
_MIN_FILE = 4096


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
            allocated = st.st_blocks * _BLOCK
            if stat.S_ISREG(st.st_mode) and st.st_size > 0:
                allocated = max(allocated, _MIN_FILE)
            entries.append(Entry(name, st.st_ino, st.st_nlink, st.st_ctime_ns, allocated))
    return entries


def group_by_inode(entries: Iterable[Entry], root: Path) -> list[Group]:
    """Join the names of each inode; refuse when fewer names were found than the link count (a writer changed links)."""
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


def _check_copy(src: Path, dst: Path, name: str, device: int) -> int:
    """Raise unless ``dst/name`` matches ``src/name`` and lives on ``device``; return the copy's inode."""
    s = os.lstat(src / name)
    try:
        d = os.lstat(dst / name)
    except FileNotFoundError:
        msg = f"{name} is missing from the copy"
        raise MigrationError(msg) from None
    if d.st_dev != device:
        msg = f"the copy of {name} is not on the staged dataset"
        raise MigrationError(msg)
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


def verify(src: Path, dst: Path, groups: Sequence[Group], *, device: int) -> tuple[list[Group], list[Group]]:
    """Split groups into verified copies and groups whose source changed since the scan.

    Any mismatch between source and copy raises, so nothing unverified is ever deleted. Every copy must be
    on ``device``, the staged dataset: a copy in the bare directory under an unmounted dataset looks complete
    but vanishes once something mounts over it or the host reboots.
    """
    verified: list[Group] = []
    changed: list[Group] = []
    for g in groups:
        if not _unchanged(src, g):
            changed.append(g)
            continue
        inodes = {_check_copy(src, dst, item.name, device) for item in g.entries}
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


def mount_device(path: Path) -> int | None:
    """Return the device of the filesystem mounted at ``path``; None when ``path`` is missing or no mount point."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_dev if st.st_dev != path.parent.stat().st_dev else None


def device_of(path: Path) -> int | None:
    """Return the device of the file at ``path``, following symlinks; None when it does not exist."""
    try:
        return path.stat().st_dev
    except (FileNotFoundError, NotADirectoryError):
        return None


MOUNTINFO = Path("/proc/self/mountinfo")
# mountinfo writes a space, tab, newline or backslash in a field as a backslash and three octal digits.
_ESCAPE = re.compile(rb"\\([0-7]{3})")


def read_mountinfo() -> bytes:
    """Return the kernel's list of the mounts this process sees."""
    return MOUNTINFO.read_bytes()


def _field(raw: bytes) -> str:
    return os.fsdecode(_ESCAPE.sub(lambda match: bytes([int(match[1], 8)]), raw))


def mounted_at(path: Path) -> tuple[str, str] | None:
    """Return the source and type of the filesystem mounted at ``path``; None when ``path`` is no mount point.

    mount resolves symlinks, so the kernel lists the real path. Of several mounts on one path, the one listed
    last covers the others.
    """
    target = os.path.realpath(path)
    found = None
    for line in read_mountinfo().splitlines():
        # ID, parent ID, device, root, mount point, options, optional fields, "-", type, source, options.
        fields = line.split(b" ")
        if _field(fields[4]) == target:
            separator = fields.index(b"-", 6)
            found = (_field(fields[separator + 2]), _field(fields[separator + 1]))
    return found


def temp_dir(dst: Path) -> Path:
    """Return the directory rsync writes each file into before it renames the file into place."""
    return dst / SCRATCH / "tmp"


def remove_partials(dst: Path) -> None:
    """Remove the partial files an rsync stopped by a power loss or SIGKILL left behind.

    They would hold their space for the rest of the move. Only the temp directory goes: the pristine
    directory copy beside it is needed until the swap.
    """
    temp = temp_dir(dst)
    if temp.is_dir():
        shutil.rmtree(temp)


def remove_scratch(dst: Path) -> None:
    """Remove the scratch directory and give the root back the pristine copy's times.

    A stop part-way must leave neither a partial pristine copy, from which a rerun would restore some
    directories and not others, nor a root whose original times are lost. So the pristine root's times go
    into a file first, one rename then hides the pristine copy from any rerun, and the file is removed only
    right before the times are restored. A rerun finishes whatever is left.
    """
    scratch = dst / SCRATCH
    if not os.path.lexists(scratch):
        return
    pristine = scratch / "dirs"
    times = scratch / "root-times"
    if pristine.is_dir():
        st = os.lstat(pristine)
        times.write_text(f"{st.st_atime_ns} {st.st_mtime_ns}\n")
        pristine.rename(scratch / "dirs.removing")
    saved = [int(part) for part in times.read_text().split()] if times.is_file() else None
    for child in scratch.iterdir():
        if child == times:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    times.unlink(missing_ok=True)
    scratch.rmdir()
    if saved is not None:
        os.utime(dst, ns=(saved[0], saved[1]))
