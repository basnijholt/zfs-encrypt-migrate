"""``file-move --check``: the run's first refusal, plus what the run would only trip over once under way."""

from __future__ import annotations

import errno
import fcntl
import os
import shlex
import stat
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import LOCK_FILE
from .errors import MigrationError, ZfsError
from .filemove import preflight_file_move, refuse_snapshots
from .filetree import mounted_at, mountinfo_field
from .status import lock_state

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .migrate import Settings
    from .zfs import Zfs

_PROC = Path("/proc")
# A process or thread that ended; EINVAL is what a zombie, or a thread whose main thread exited, gives.
_GONE = {errno.ENOENT, errno.ESRCH, errno.EINVAL}
_FLAGS = ((0x10, "i"), (0x20, "a"))  # FS_IMMUTABLE_FL, FS_APPEND_FL
_GETFLAGS = 0x80086601  # FS_IOC_GETFLAGS, _IOR('f', 1, long) on 64-bit Linux
# Read-only without touching access times; non-blocking, so a fifo swapped in, or a leased file, fails instead of
# hanging the check. Root, which the check needs, may use O_NOATIME on any file.
_OPEN = os.O_RDONLY | os.O_NOATIME | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC


def _shown(name: str) -> str:
    """Return ``name`` printable: a control character or a byte that is no UTF-8 shows as an escape."""
    text = os.fsencode(name).decode(errors="backslashreplace")
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)


def _incomplete(what: str, first: tuple[str, OSError], hint: str) -> MigrationError:
    """Report a scan left incomplete, naming the first entry it missed and why."""
    name, reason = first
    return MigrationError(f"{what}, such as {_shown(name)}: {reason.strerror or reason}", [hint])


def _snapshots(raw: bytes, dataset: str) -> Iterator[tuple[str, str]]:
    """Yield the source and mount point of each mount of a snapshot of ``dataset`` in a mountinfo."""
    # The kernel escapes a newline in a path but not a carriage return, which splitlines() would split on.
    for line in filter(None, raw.split(b"\n")):
        # ID, parent ID, device, root, mount point, options, optional fields, "-", type, source, options.
        fields = line.split(b" ")
        end = fields.index(b"-", 6)
        source = mountinfo_field(fields[end + 2])
        if fields[end + 1] == b"zfs" and source.startswith(f"{dataset}@"):
            yield source, mountinfo_field(fields[4])


def _mountinfo(task: Path) -> bytes:
    try:
        return (task / "mountinfo").read_bytes()
    except OSError as error:
        if error.errno in _GONE:
            return b""
        raise


def _snapshot_mounts(proc: Path, dataset: str) -> list[MigrationError]:
    """Return a problem for each snapshot of ``dataset`` mounted in any mount namespace that has a process.

    Every thread's mountinfo is read, as each leaves out mounts outside its root; one unreadable is a problem too.
    """
    seen: dict[tuple[str, str], str] = {}
    failed: list[tuple[str, OSError]] = []
    for pid in sorted((entry.name for entry in os.scandir(proc) if entry.name.isdigit()), key=int):
        try:
            with os.scandir(proc / pid / "task") as tasks:
                infos = [_mountinfo(Path(task.path)) for task in tasks]
        except OSError as error:
            if error.errno not in _GONE:
                failed.append((f"PID {pid}", error))
            continue
        for raw in infos:
            for mount in _snapshots(raw, dataset):
                seen.setdefault(mount, pid)
    hint = "unmount it, or stop the processes or containers that hold it: zfs destroy cannot remove a mounted snapshot"
    problems = [
        MigrationError(f"snapshot {source} is mounted at {_shown(point)}, as seen by PID {pid}", [hint])
        for (source, point), pid in seen.items()
    ]
    if failed:
        unknown = "check again; until then, snapshots mounted there are unknown"
        problems.append(_incomplete("could not read the mounts of some processes", failed[0], unknown))
    return problems


def _inode_flags(fd: int) -> int:
    """Return the inode flags of the open file ``fd``, as lsattr reads them."""
    # The kernel writes an int, though the request is declared with a long.
    flags: int = struct.unpack_from("I", fcntl.ioctl(fd, _GETFLAGS, bytes(8)))[0]
    return flags


def _letters(path: str, top: bool, device: int, visited: set[tuple[int, int]], pending: list[str]) -> str:  # noqa: FBT001
    """Return the entry's immutable and append-only letters; queue a directory's entries, entering each once."""
    fd = os.open(path, _OPEN)
    try:
        st = os.fstat(fd)
        if st.st_dev != device or (st.st_dev, st.st_ino) in visited:
            return ""
        flags = _inode_flags(fd)
        if stat.S_ISDIR(st.st_mode):
            visited.add((st.st_dev, st.st_ino))
            with os.scandir(fd) as it:
                pending += [
                    f"{path.rstrip('/')}/{entry.name}"
                    for entry in it
                    if (entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False))
                    and not (top and entry.name == ".zfs")
                ]
    finally:
        os.close(fd)
    return "".join(letter for bit, letter in _FLAGS if flags & bit)


def _pinned_files(root: Path, dataset: str) -> list[MigrationError]:
    """Return a problem for the immutable or append-only entries under ``root``: the run would stop at the first.

    Like lsattr it reads files and directories, not symlinks, on the dataset only; one unread or changed is a problem.
    """
    commands: list[tuple[str, str]] = []
    unread: list[tuple[str, OSError]] = []
    changed: list[tuple[str, OSError]] = []
    visited: set[tuple[int, int]] = set()
    device, pending = os.lstat(root).st_dev, [str(root)]
    while pending:
        path = pending.pop()
        try:
            letters = _letters(path, path == str(root), device, visited, pending)
        except FileNotFoundError as error:
            changed.append((path, error))
            continue
        except OSError as error:
            unread.append((path, error))
            continue
        if letters and not path.isprintable():
            # Quoting the escape would name another file.
            fix = f"chattr -{letters}, and chattr +{letters} after the swap"
            commands.append((path, f"  {_shown(path)}: its name is not printable, so fix it by hand: {fix}"))
        elif letters:
            quoted = shlex.quote(path)
            commands.append((path, f"  chattr -{letters} {quoted}  # after the swap: chattr +{letters} {quoted}"))
    problems = []
    if commands:
        message = f"{dataset} has files or directories with the immutable or append-only flag, which keeps file-move "
        hint = "clear the flags; the copies get none, so set them again after the swap:"
        hints = [hint, *(command for _, command in sorted(commands))]
        problems.append(MigrationError(message + "from deleting them or the files in them", hints))
    if unread:
        hint = "check them by hand with lsattr -d; the run stops at an immutable or append-only one"
        what = f"could not read the inode flags of some entries in {dataset}"
        problems.append(_incomplete(what, min(unread, key=lambda item: item[0]), hint))
    if changed:
        what = f"some entries in {dataset} changed during the check"
        problems.append(_incomplete(what, min(changed, key=lambda item: item[0]), "stop the writers and check again"))
    return problems


def check_file_move(
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    *,
    proc: Path = _PROC,
    lock_path: Path = LOCK_FILE,
) -> list[MigrationError]:
    """Return what would make file-move refuse or stall, in the categories checked; advisory, the run checks again.

    It makes no ZFS changes and takes no locks, and reads nothing else while a run holds, or may hold, its lock.
    """
    if os.geteuid() != 0:
        return [MigrationError("run --check as root, like file-move: without root it cannot see every mount and file")]
    state = lock_state(lock_path)
    if state != "not held":
        hint = f"its lock is {state}; check again once no run holds it"
        return [MigrationError("a file-move is running; check skipped", [hint])]
    problems: list[MigrationError] = []
    try:
        # With snapshots the preflight would scan the dataset, which can update directory access times.
        if zfs.exists(dataset) and zfs.snapshots(dataset):
            try:
                refuse_snapshots(zfs, dataset)
            except ZfsError:
                raise
            except MigrationError as refusal:
                late = (
                    "the run refuses this while files remain; "
                    "once only directories are left, after a stopped swap, it allows it"
                )
                raise MigrationError(refusal.message, [*refusal.hints, late]) from None
        preflight_file_move(zfs, settings, dataset)
    except MigrationError as refusal:
        if isinstance(refusal, ZfsError):
            raise
        problems.append(refusal)
    problems += _snapshot_mounts(proc, dataset)
    if zfs.exists(dataset):
        mountpoint = zfs.get("mountpoint", dataset)
        # zfs get strips the spaces around a mountpoint, so the path it gives may be another directory.
        if mountpoint.startswith("/") and mounted_at(Path(mountpoint)) == (dataset, "zfs"):
            problems += _pinned_files(Path(mountpoint), dataset)
        else:
            message = f"could not check flags: {dataset} is not mounted at {mountpoint}"
            problems.append(MigrationError(message, ["mount it there, then check again"]))
    return problems
