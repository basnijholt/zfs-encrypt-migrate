"""``file-move --check``: the run's first refusal, plus what the run would only trip over once under way."""

from __future__ import annotations

import errno
import fcntl
import os
import shlex
import stat
import struct
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from .constants import LOCK_FILE
from .errors import MigrationError, ZfsError
from .filemove import preflight_file_move
from .filetree import mounted_at, mountinfo_field
from .status import lock_state

if TYPE_CHECKING:
    from .migrate import Settings
    from .zfs import Zfs

_PROC = Path("/proc")
_FLAGS = ((0x10, "i"), (0x20, "a"))  # FS_IMMUTABLE_FL, FS_APPEND_FL
# FS_IOC_GETFLAGS is _IOR('f', 1, long) in the generic ioctl encoding, which x86 and arm use.
_GETFLAGS = (2 << 30) | (struct.calcsize("l") << 16) | (ord("f") << 8) | 1
# Non-blocking, so a fifo swapped in, or a leased file, fails instead of hanging the check.
_OPEN = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC


def _shown(name: str) -> str:
    """Return ``name`` printable: a byte that is no UTF-8 shows as an escape instead of stopping the output."""
    return os.fsencode(name).decode(errors="backslashreplace")


class _Mount(NamedTuple):
    mount_id: int
    parent: int
    point: str
    fstype: str
    source: str


def _mounts(raw: bytes) -> list[_Mount]:
    mounts = []
    # The kernel escapes a newline in a path but not a carriage return, which splitlines() would split on.
    for line in filter(None, raw.split(b"\n")):
        # ID, parent ID, device, root, mount point, options, optional fields, "-", type, source, options.
        fields = line.split(b" ")
        end = fields.index(b"-", 6)
        point, fstype, source = (mountinfo_field(fields[i]) for i in (4, end + 1, end + 2))
        mounts.append(_Mount(int(fields[0]), int(fields[1]), point, fstype, source))
    return mounts


def _comm(proc: Path, pid: str) -> str:
    try:
        return f" ({_shown(os.fsdecode((proc / pid / 'comm').read_bytes().strip()))})"
    except OSError:
        return ""


def _snapshot_mounts(proc: Path, dataset: str) -> list[MigrationError]:
    """Return a problem for each snapshot of ``dataset`` mounted in any mount namespace, as any thread sees it.

    A thread's mountinfo leaves out what lies outside its root, as in a chroot, so every thread's is read. One that
    is gone is skipped; one that cannot be read leaves the scan incomplete, which is a problem too.
    """
    seen: dict[tuple[str, str], list[str]] = {}
    failed: list[tuple[str, OSError]] = []
    for path in sorted(proc.glob("[0-9]*/task/[0-9]*/mountinfo"), key=lambda p: (int(p.parts[-4]), int(p.parts[-2]))):
        try:
            mounts = _mounts(path.read_bytes())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            failed.append((path.parts[-4], error))
            continue
        for mount in mounts:
            if mount.fstype == "zfs" and mount.source.startswith(f"{dataset}@"):
                seen.setdefault((mount.source, mount.point), []).append(path.parts[-4])
    me, problems = (proc / "self").readlink().name, []
    for (source, point), pids in seen.items():
        message = f"snapshot {source} is mounted at {_shown(point)}"
        if me in pids:
            problems.append(MigrationError(message, [f"unmount it: umount {shlex.quote(_shown(point))}"]))
        else:
            hint = (
                "stop the processes or containers that hold it: a mounted snapshot keeps zfs destroy from removing it"
            )
            problems.append(MigrationError(f"{message}, as seen by PID {pids[0]}{_comm(proc, pids[0])}", [hint]))
    if failed:
        (pid, reason), count = failed[0], len({pid for pid, _ in failed})
        such = f"such as PID {pid}{_comm(proc, pid)}: {reason.strerror or reason}"
        message = f"could not read the mounts of {count} process{'es' if count > 1 else ''}, {such}"
        problems.append(MigrationError(message, ["check again; until then, snapshots mounted there are unknown"]))
    return problems


def _submounts(proc: Path, dataset: str, mountpoint: Path) -> list[MigrationError]:
    """Return a problem for each filesystem mounted on the dataset here, which the run's unmount fails on."""
    mounts = _mounts((proc / "self" / "mountinfo").read_bytes())
    target = os.path.realpath(mountpoint)
    # Of several mounts on one path, the one listed last covers the others.
    own = [mount.mount_id for mount in mounts if (mount.fstype, mount.source, mount.point) == ("zfs", dataset, target)]
    busy = f"inside {dataset}, which makes the run's unmount fail with busy"
    return [
        MigrationError(
            f"{mount.fstype} {_shown(mount.source)} is mounted at {_shown(mount.point)}, {busy}",
            [f"unmount it: umount {shlex.quote(_shown(mount.point))}"],
        )
        for mount in mounts
        # A snapshot of the dataset is reported with the others, wherever it is mounted.
        if own and mount.parent == own[-1] and not mount.source.startswith(f"{dataset}@")
    ]


def _inode_flags(fd: int) -> int:
    """Return the inode flags of the open file ``fd``, as lsattr reads them."""
    # The kernel writes an int, though the request is declared with a long.
    flags: int = struct.unpack_from("I", fcntl.ioctl(fd, _GETFLAGS, bytes(struct.calcsize("l"))))[0]
    return flags


def _open(path: str) -> int:
    """Open ``path`` without updating its access time; without O_NOATIME only where the kernel refuses it."""
    try:
        return os.open(path, _OPEN | os.O_NOATIME)
    except PermissionError as error:
        if error.errno != errno.EPERM:
            raise
        return os.open(path, _OPEN)


def _letters(path: str, top: bool, device: int, visited: set[tuple[int, int]], pending: list[str]) -> str:  # noqa: FBT001
    """Return the entry's immutable and append-only letters; queue a directory's entries, entering each once."""
    fd = _open(path)
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
        if letters:
            quoted = shlex.quote(_shown(path))
            commands.append((path, f"  chattr -{letters} {quoted}  # after the swap: chattr +{letters} {quoted}"))
    problems = []
    if commands:
        message = f"{dataset} has files or directories with the immutable or append-only flag, which keeps file-move "
        hints = [
            "clear the flags; the copies get none, so set them again after the swap:",
            *dict(sorted(commands)).values(),
        ]
        problems.append(MigrationError(message + "from deleting them or the files in them", hints))
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

    The run's preflight gives its first refusal; then come snapshot mounts, filesystems mounted inside the dataset and
    files the run cannot delete. While a run holds, or may hold, its lock, it reads nothing else.
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
    source = Path(zfs.get("mountpoint", dataset)) if zfs.exists(dataset) else None
    # zfs get strips the spaces around a mountpoint, so the path it gives may be another directory.
    if source is not None and source.is_absolute() and mounted_at(source) == (dataset, "zfs"):
        problems += _submounts(proc, dataset, source) + _pinned_files(source, dataset)
    return problems
