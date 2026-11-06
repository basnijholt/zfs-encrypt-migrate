"""``file-move --check``: the run's first refusal, plus what the run would only trip over once under way."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shlex
import struct
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

from .constants import LOCK_FILE
from .errors import MigrationError, ZfsError
from .filemove import preflight_file_move
from .filetree import mounted_at, mountinfo_field

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .migrate import Settings
    from .zfs import Zfs

_PROC = Path("/proc")
_STOP = (
    "stop it, and keep whatever starts it again (a unit, a container's restart policy) disabled until file-move is done"
)
_LINKS = (("cwd", "working directory"), ("root", "root directory"), ("exe", "program"))
_FLAGS = ((0x10, "i"), (0x20, "a"))  # FS_IMMUTABLE_FL, FS_APPEND_FL
# FS_IOC_GETFLAGS is _IOR('f', 1, long) in the generic ioctl encoding, which x86 and arm use.
_GETFLAGS = (2 << 30) | (struct.calcsize("l") << 16) | (ord("f") << 8) | 1


class _Mount(NamedTuple):
    mount_id: int
    parent: int
    root: str
    point: str
    shared: int | None
    master: int | None
    fstype: str
    source: str


def _mounts(raw: bytes) -> list[_Mount]:
    mounts = []
    # The kernel escapes a newline in a path but not a carriage return, which splitlines() would split on.
    for line in filter(None, raw.split(b"\n")):
        # ID, parent ID, device, root, mount point, options, optional fields, "-", type, source, options.
        fields = line.split(b" ")
        end = fields.index(b"-", 6)
        tags = {name: value for name, _, value in (field.partition(b":") for field in fields[6:end])}
        shared, master = (int(tags[tag]) if tag in tags else None for tag in (b"shared", b"master"))
        root, point, fstype, source = (mountinfo_field(fields[i]) for i in (3, 4, end + 1, end + 2))
        mounts.append(_Mount(int(fields[0]), int(fields[1]), root, point, shared, master, fstype, source))
    return mounts


# Each other mount namespace: a process in it, its mounts, and whether they are locked to another user namespace.
_Namespaces = dict[str, tuple[Path, list[_Mount], bool]]


def _of(mount: _Mount, dataset: str) -> bool:
    return mount.fstype == "zfs" and (mount.source == dataset or mount.source.startswith(f"{dataset}@"))


def _place(mount: _Mount, mounts: dict[int, _Mount]) -> tuple[str, ...] | None:
    """Return what ``mount`` is and where it sits in its parent's filesystem, which its copies share."""
    parent = mounts.get(mount.parent)
    with contextlib.suppress(ValueError):
        if parent is not None:
            place = PurePosixPath(parent.root) / PurePosixPath(mount.point).relative_to(parent.point)
            return mount.fstype, mount.source, mount.root, str(place)
    return None


def _origin(copy: _Mount, ours: list[_Mount], mounts: dict[int, _Mount]) -> _Mount | None:
    place = _place(copy, mounts)
    return next((mount for mount in ours if place is not None and _place(mount, mounts) == place), None)


def _left(copy: _Mount, ours: list[_Mount], mounts: dict[int, _Mount], dataset: str, *, locked: bool) -> str | None:
    """Return how the run's unmount leaves ``copy``, in another namespace, mounted; None when it takes it along.

    The kernel passes an unmount on to the peers and slaves of the parent mount, and unmounts there what sits at the
    same place, unless it is locked to another user namespace or holds mounts of its own that are not going too.
    """
    if locked:
        return "in another user namespace, where it is locked and file-move's unmount does not reach it"
    origin, parent = _origin(copy, ours, mounts), mounts.get(copy.parent)
    group = mounts[origin.parent].shared if origin is not None else None
    if group is None or parent is None or (parent.shared, parent.master) == (None, None):
        return "in another mount namespace, which file-move's unmount does not reach"
    if group not in {parent.shared, parent.master}:
        return (
            "in another mount namespace, copied there through another one; file-move's unmount may still reach it, "
            "but the check cannot tell"
        )
    if any(child.parent == copy.mount_id and _origin(child, ours, mounts) is None for child in mounts.values()):
        return (
            f"in another mount namespace, with other mounts on it there that keep it mounted when file-move unmounts "
            f"{dataset}"
        )
    return None


def _label(base: Path) -> str:
    """Name the process at ``base`` by PID, command and, when known, cgroup."""
    label = f"PID {base.name}"
    with contextlib.suppress(OSError):
        label += f" ({(base / 'comm').read_text().strip()})"
    with contextlib.suppress(OSError):
        lines = (base / "cgroup").read_text().split("\n")
        label += next((f" in {line[3:]}" for line in lines if line.startswith("0::") and line != "0::/"), "")
    return label


def _here(mount: _Mount, dataset: str, own: int | None) -> MigrationError:
    """Describe a mount in this namespace that keeps the dataset busy, reachable, or its snapshot from going."""
    point = shlex.quote(mount.point)
    if mount.fstype == "zfs" and mount.source == dataset:
        message = f"{dataset} is also mounted at {mount.point}, which file-move's unmount does not reach"
        return MigrationError(message, [_STOP, f"or unmount it: umount {point}"])
    what = f"snapshot {mount.source}" if _of(mount, dataset) else f"{mount.fstype} {mount.source}"
    inside = f"inside {dataset}, which makes file-move's unmount fail with busy"
    effect = inside if mount.parent == own else "which keeps zfs destroy from removing it"
    return MigrationError(f"{what} is mounted at {mount.point}, {effect}", [f"unmount it: umount {point}"])


def _mount_problems(proc: Path, dataset: str, mountpoint: Path, others: _Namespaces) -> list[MigrationError]:
    """Return a problem for each mount that the run's unmount of the dataset fails on, or leaves in place."""
    ours = _mounts((proc / "self" / "mountinfo").read_bytes())
    # Mount IDs are unique across namespaces.
    every = {mount.mount_id: mount for mount in ours} | {m.mount_id: m for _, ms, _ in others.values() for m in ms}
    target = os.path.realpath(mountpoint)
    # Of several mounts on one path, the one listed last covers the others.
    own = next((m for m in reversed(ours) if m.fstype == "zfs" and m.source == dataset and m.point == target), None)
    own_id = own.mount_id if own is not None else None
    problems = [_here(m, dataset, own_id) for m in ours if m is not own and (_of(m, dataset) or m.parent == own_id)]
    for base, mounts, locked in others.values():
        for mount in mounts:
            if not _of(mount, dataset) or not (reason := _left(mount, ours, every, dataset, locked=locked)):
                continue
            if mount.source != dataset:
                reason = "in another mount namespace, which keeps zfs destroy from removing it"
            what = dataset if mount.source == dataset else f"snapshot {mount.source}"
            problems.append(MigrationError(f"{_label(base)} has {what} mounted at {mount.point} {reason}", [_STOP]))
    return problems


def _uses(base: Path, device: int) -> list[str]:
    """Describe what of the process at ``base`` is on ``device``: directories, program, open and mapped files."""
    links = [(base / name, what) for name, what in _LINKS]
    with contextlib.suppress(FileNotFoundError, ProcessLookupError):
        links += [(fd, "open file") for fd in sorted((base / "fd").iterdir(), key=lambda fd: int(fd.name))]
    uses = []
    for link, what in links:
        with contextlib.suppress(FileNotFoundError, ProcessLookupError):
            if link.stat().st_dev == device:
                uses.append(f"{what} {link.readlink()}")
    maps: list[bytes] = []
    with contextlib.suppress(FileNotFoundError, ProcessLookupError):
        maps = (base / "maps").read_bytes().split(b"\n")
    mapped: dict[str, None] = {}
    for line in maps:
        # Address, permissions, offset, device, inode, path.
        fields = line.split(b" ", 5)
        if len(fields) < 6 or fields[4] == b"0":  # noqa: PLR2004
            continue
        major, _, minor = fields[3].partition(b":")
        if (int(major, 16), int(minor, 16)) == (os.major(device), os.minor(device)):
            mapped.setdefault(f"mapped file {os.fsdecode(fields[5].lstrip(b' '))}", None)
    return uses + list(mapped)


def _holders(proc: Path, dataset: str, mountpoint: Path) -> list[MigrationError]:
    """Return a problem for each process, in any namespace, and each mount that keeps the dataset busy or mounted."""
    device = mountpoint.stat().st_dev
    me = (proc / "self").readlink().name
    ours = str((proc / "self" / "ns" / "mnt").readlink())
    init_user = str((proc / "1" / "ns" / "user").readlink())
    others: _Namespaces = {}
    users: list[MigrationError] = []
    for pid in sorted(int(entry.name) for entry in proc.iterdir() if entry.name.isdigit() and entry.name != me):
        base = proc / str(pid)
        try:
            namespace = str((base / "ns" / "mnt").readlink())
            uses = _uses(base, device)
            if namespace != ours and namespace not in others:
                locked = str((base / "ns" / "user").readlink()) != init_user
                others[namespace] = (base, _mounts((base / "mountinfo").read_bytes()), locked)
        except (FileNotFoundError, ProcessLookupError):
            continue
        if uses:
            where = " from another mount namespace" if namespace != ours else ""
            more = f", and {len(uses) - 1} more" if len(uses) > 1 else ""
            users.append(MigrationError(f"{_label(base)} uses {dataset}{where}: {uses[0]}{more}", [_STOP]))
    return _mount_problems(proc, dataset, mountpoint, others) + users


def _inode_flags(path: str) -> int:
    """Return the inode flags of a regular file or directory, as lsattr reads them, without following a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        # The kernel writes an int, though the request is declared with a long.
        flags: int = struct.unpack_from("I", fcntl.ioctl(fd, _GETFLAGS, bytes(struct.calcsize("l"))))[0]
    finally:
        os.close(fd)
    return flags


def _entries(root: Path, failed: list[tuple[str, OSError]]) -> Iterator[str]:
    """Yield the directories and regular files on the dataset under ``root``, as lsattr reads; skip a root .zfs."""
    device = os.lstat(root).st_dev
    pending = [str(root)]
    while pending:
        directory = pending.pop()
        yield directory
        try:
            with os.scandir(directory) as it:
                children = [child for child in it if directory != str(root) or child.name != ".zfs"]
        except FileNotFoundError:
            continue
        except OSError as error:
            failed.append((directory, error))
            continue
        for child in children:
            with contextlib.suppress(FileNotFoundError):
                if child.is_dir(follow_symlinks=False) and os.lstat(child.path).st_dev == device:
                    pending.append(child.path)
                elif child.is_file(follow_symlinks=False):
                    yield child.path


def _pinned_files(root: Path, dataset: str) -> list[MigrationError]:
    """Return a problem for the immutable or append-only entries under ``root``: the run would stop at the first."""
    failed: list[tuple[str, OSError]] = []
    commands: list[tuple[str, str]] = []
    for path in _entries(root, failed):
        try:
            flags = _inode_flags(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            failed.append((path, error))
            continue
        if letters := "".join(letter for bit, letter in _FLAGS if flags & bit):
            quoted = shlex.quote(path)
            commands.append((path, f"  chattr -{letters} {quoted}  # after the swap: chattr +{letters} {quoted}"))
    problems = []
    if commands:
        message = f"{dataset} has files or directories with the immutable or append-only flag, which keeps file-move "
        hints = ["clear the flags; the copies get none, so set them again after the swap:"]
        hints += [command for _, command in sorted(commands)]
        problems.append(MigrationError(message + "from deleting them or the files in them", hints))
    # A lease, from an NFS delegation or a Samba oplock, refuses a non-blocking open.
    if leased := sorted(path for path, error in failed if isinstance(error, BlockingIOError)):
        message = (
            f"{dataset} has leased files, such as {leased[0]}: an NFS client or Samba holds a delegation or oplock"
        )
        hint = "stop the NFS server and the Samba exports of the dataset; the run would wait on each leased file"
        problems.append(MigrationError(message + " on them", [hint]))
    if unread := sorted((item for item in failed if not isinstance(item[1], BlockingIOError)), key=lambda i: i[0]):
        first, reason = unread[0]
        count = f"{len(unread)} entr{'ies' if len(unread) > 1 else 'y'}"
        message = (
            f"could not read the inode flags of {count} in {dataset}, such as {first}: {reason.strerror or reason}"
        )
        hint = "check them by hand with lsattr -d; the run stops at an immutable or append-only one"
        problems.append(MigrationError(message, [hint]))
    return problems


def _running(proc: Path, lock: Path) -> bool:
    """Return whether a run holds ``lock``: its device and inode on a FLOCK line of /proc/locks, not a waiter's."""
    if not lock.exists():
        return False
    st = lock.stat()
    held = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    lines = (proc / "locks").read_text().split("\n")
    return any(fields[1:2] == ["FLOCK"] and fields[5:6] == [held] for fields in map(str.split, lines))


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
    """Return the problems that would make file-move refuse or stall.

    It changes no ZFS state, mount or lock, and opens files only read-only, for their flags. The run's own preflight
    gives its first refusal; then come what keeps the dataset busy or mounted, and files the run cannot delete. While a
    run holds its lock, it reads nothing else.
    """
    if not _is_root():
        return [
            MigrationError("run --check as root, like file-move: without root it cannot see every process and file"),
        ]
    if _running(proc, lock_path):
        return [MigrationError("a file-move is running; check skipped", ["check again once it has stopped"])]
    problems: list[MigrationError] = []
    try:
        preflight_file_move(zfs, settings, dataset)
    except MigrationError as refusal:
        if isinstance(refusal, ZfsError):
            raise
        problems.append(refusal)
    source = Path(zfs.get("mountpoint", dataset)) if zfs.exists(dataset) else None
    # zfs get strips the spaces around a mountpoint, so the path it gives may be another directory.
    if source is not None and source.is_absolute() and mounted_at(source) == (dataset, "zfs"):
        problems += _pinned_files(source, dataset) + _holders(proc, dataset, source)
    return problems
