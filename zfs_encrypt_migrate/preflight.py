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
from .filemove import preflight_file_move
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
# FS_IOC_GETFLAGS is _IOR('f', 1, long) in the generic ioctl encoding, which x86 and arm use.
_GETFLAGS = (2 << 30) | (struct.calcsize("l") << 16) | (ord("f") << 8) | 1
# Read-only without touching access times; non-blocking, so a fifo swapped in, or a leased file, fails instead of
# hanging the check. Root, which the check needs, may use O_NOATIME on any file.
_OPEN = os.O_RDONLY | os.O_NOATIME | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC


def _shown(name: str) -> str:
    """Return ``name`` printable: a byte that is no UTF-8 shows as an escape instead of stopping the output."""
    return os.fsencode(name).decode(errors="backslashreplace")


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

    Every thread's mountinfo is read: each leaves out mounts outside the thread's root, as in a chroot. A task that is
    gone is skipped; one that cannot be read or listed leaves the scan incomplete, which is a problem too.
    """
    seen: dict[tuple[str, str], str] = {}
    failed: list[tuple[str, OSError]] = []
    for pid in sorted((entry.name for entry in os.scandir(proc) if entry.name.isdigit()), key=int):
        try:
            with os.scandir(proc / pid / "task") as tasks:
                infos = [_mountinfo(Path(task.path)) for task in tasks]
        except OSError as error:
            if error.errno not in _GONE:
                failed.append((pid, error))
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
        (pid, reason), count = failed[0], len(failed)
        such = f"such as PID {pid}: {reason.strerror or reason}"
        message = f"could not read the mounts of {count} process{'es' if count > 1 else ''}, {such}"
        problems.append(MigrationError(message, ["check again; until then, snapshots mounted there are unknown"]))
    return problems


def _inode_flags(fd: int) -> int:
    """Return the inode flags of the open file ``fd``, as lsattr reads them."""
    # The kernel writes an int, though the request is declared with a long.
    flags: int = struct.unpack_from("I", fcntl.ioctl(fd, _GETFLAGS, bytes(struct.calcsize("l"))))[0]
    return flags


def _letters(path: str, top: bool, device: int, visited: set[tuple[int, int]], pending: list[str]) -> str:  # noqa: FBT001
    """Return the entry's immutable and append-only letters; queue a directory's entries, entering each once."""
    fd = os.open(path, _OPEN)
    try:
        st = os.fstat(fd)
        if st.st_dev != device or (st.st_dev, st.st_ino) in visited:
            return ""
        visited.add((st.st_dev, st.st_ino))
        flags = _inode_flags(fd)
        if stat.S_ISDIR(st.st_mode):
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

    Like lsattr it reads regular files and directories, never follows a symlink, stays on the dataset's device and
    skips a top-level ``.zfs``. An entry it cannot read is a problem too.
    """
    commands: list[tuple[str, str]] = []
    unread: list[tuple[str, OSError]] = []
    visited: set[tuple[int, int]] = set()
    device, pending = os.lstat(root).st_dev, [str(root)]
    while pending:
        path = pending.pop()
        try:
            letters = _letters(path, path == str(root), device, visited, pending)
        except FileNotFoundError:
            continue
        except OSError as error:
            unread.append((path, error))
            continue
        if letters and _shown(path) != path:
            # Quoting the escape would name another file.
            fix = f"chattr -{letters}, and chattr +{letters} after the swap"
            fix = f"its name is no valid UTF-8, so fix it by hand: {fix}"
            commands.append((path, f"  {_shown(path)}: {fix}"))
        elif letters:
            quoted = shlex.quote(path)
            commands.append((path, f"  chattr -{letters} {quoted}  # after the swap: chattr +{letters} {quoted}"))
    problems = []
    if commands:
        message = f"{dataset} has files or directories with the immutable or append-only flag, which keeps file-move "
        hints = ["clear the flags; the copies get none, so set them again after the swap:"]
        problems.append(
            MigrationError(
                message + "from deleting them or the files in them",
                hints + [c for _, c in sorted(commands)],
            ),
        )
    if unread:
        (first, reason), count = min(unread, key=lambda item: item[0]), len(unread)
        such = f"such as {_shown(first)}: {reason.strerror or reason}"
        message = f"could not read the inode flags of {count} entr{'ies' if count > 1 else 'y'} in {dataset}, {such}"
        hint = "check them by hand with lsattr -d; the run stops at an immutable or append-only one"
        problems.append(MigrationError(message, [hint]))
    return problems


def _is_root() -> bool:
    return os.geteuid() == 0


def check_file_move(
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    *,
    proc: Path = _PROC,
    lock_path: Path = LOCK_FILE,
) -> list[MigrationError]:
    """Return what would make file-move refuse or stall, in the categories checked; change no ZFS state, mount or lock.

    The run's preflight gives its first refusal; then come snapshot mounts and the files the run cannot delete. While
    a run holds, or may hold, its lock, it reads nothing else.
    """
    if not _is_root():
        return [MigrationError("run --check as root, like file-move: without root it cannot see every mount and file")]
    state = lock_state(lock_path)
    if state != "not held":
        hint = f"its lock is {state}; check again once no run holds it"
        return [MigrationError("a file-move is running; check skipped", [hint])]
    problems: list[MigrationError] = []
    try:
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
