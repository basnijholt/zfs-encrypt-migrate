"""``file-move --check``: the run's first refusal, plus what the run would only trip over once under way."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shlex
import struct
from dataclasses import dataclass
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
_AS_ROOT = "run the check as root, like file-move itself"
_STOP = (
    "stop it, and keep whatever starts it again (a unit, a container's restart policy) disabled until file-move is done"
)
_LINKS = (("cwd", "working directory"), ("root", "root directory"), ("exe", "program"))
_FLAGS = ((0x10, "i"), (0x20, "a"))  # FS_IMMUTABLE_FL, FS_APPEND_FL
# FS_IOC_GETFLAGS is _IOR('f', 1, long) in the generic ioctl encoding, which x86 and arm use.
_GETFLAGS = (2 << 30) | (struct.calcsize("l") << 16) | (ord("f") << 8) | 1


@dataclass(frozen=True)
class CheckReport:
    """What ``file-move --check`` found: problems that would stop the run, and notes that would not."""

    problems: list[MigrationError]
    notes: list[MigrationError]


class _Mount(NamedTuple):
    mount_id: int
    parent: int
    root: str
    point: str
    shared: int | None
    master: int | None
    source: str  # empty unless the filesystem is zfs


def _mounts(raw: bytes) -> list[_Mount]:
    mounts = []
    for line in raw.splitlines():
        # ID, parent ID, device, root, mount point, options, optional fields, "-", type, source, options.
        fields = line.split(b" ")
        end = fields.index(b"-", 6)
        tags = {name: value for name, _, value in (field.partition(b":") for field in fields[6:end])}
        shared, master = (int(tags[tag]) if tag in tags else None for tag in (b"shared", b"master"))
        source = mountinfo_field(fields[end + 2]) if fields[end + 1] == b"zfs" else ""
        root, point = mountinfo_field(fields[3]), mountinfo_field(fields[4])
        mounts.append(_Mount(int(fields[0]), int(fields[1]), root, point, shared, master, source))
    return mounts


# Each other mount namespace: a process in it and its mounts.
_Namespaces = dict[str, tuple[Path, list[_Mount]]]


def _propagated(origin: _Mount, copy: _Mount, mounts: dict[int, _Mount]) -> bool:
    """Return whether unmounting ``origin`` unmounts ``copy``: the same place under a peer or slave of its parent."""
    parent, other = mounts.get(origin.parent), mounts.get(copy.parent)
    if parent is None or other is None or parent.shared is None or origin.root != copy.root:
        return False
    try:
        places = {
            str(PurePosixPath(p.root) / PurePosixPath(m.point).relative_to(p.point))
            for m, p in ((origin, parent), (copy, other))
        }
    except ValueError:
        return False
    return len(places) == 1 and parent.shared in {other.shared, other.master}


def _label(base: Path) -> str:
    """Name the process at ``base`` by PID, command and, when known, cgroup."""
    label = f"PID {base.name}"
    with contextlib.suppress(OSError):
        label += f" ({(base / 'comm').read_text().strip()})"
    with contextlib.suppress(OSError):
        lines = (base / "cgroup").read_text().splitlines()
        label += next((f" in {line[3:]}" for line in lines if line.startswith("0::") and line != "0::/"), "")
    return label


def _mount_problem(mount: _Mount, dataset: str, base: Path | None) -> MigrationError:
    """Describe a mount the unmount leaves; ``base`` is a process of its namespace, None for this one."""
    point = shlex.quote(mount.point)
    unmount = f"nsenter -t {base.name} -m umount {point}" if base else f"umount {point}"
    where = f" in the mount namespace of {_label(base)}" if base else ""
    if mount.source != dataset:
        message = f"snapshot {mount.source} is mounted at {mount.point}{where}, which keeps {dataset} busy"
        return MigrationError(message, [f"unmount it, before you destroy the snapshot: {unmount}"])
    holder = f"{_label(base)} has {dataset} mounted at {mount.point} in another mount namespace" if base else ""
    message = f"{holder or f'{dataset} is also mounted at {mount.point}'}, which file-move's unmount does not reach"
    return MigrationError(message, [_STOP, f"or unmount it: {unmount}"])


def _mount_problems(proc: Path, dataset: str, mountpoint: Path, others: _Namespaces) -> list[MigrationError]:
    """Return a problem for each mount of the dataset, or a snapshot of it, that the run's unmount leaves in place."""
    ours = _mounts((proc / "self" / "mountinfo").read_bytes())
    # Mount IDs are unique across namespaces.
    every = {mount.mount_id: mount for mount in ours} | {m.mount_id: m for _, ms in others.values() for m in ms}
    mine = [mount for mount in ours if mount.source == dataset or mount.source.startswith(f"{dataset}@")]
    target = os.path.realpath(mountpoint)
    # Of several mounts on one path, the one listed last covers the others.
    own = next((mount for mount in reversed(mine) if mount.source == dataset and mount.point == target), None)
    problems = [_mount_problem(mount, dataset, None) for mount in mine if mount is not own]
    for base, mounts in others.values():
        problems += [
            _mount_problem(mount, dataset, base)
            for mount in mounts
            if (mount.source == dataset or mount.source.startswith(f"{dataset}@"))
            and not any(origin.source == mount.source and _propagated(origin, mount, every) for origin in mine)
        ]
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
        maps = (base / "maps").read_bytes().splitlines()
    mapped: dict[str, None] = {}
    for line in maps:
        # Address, permissions, offset, device, inode, path.
        fields = line.split(maxsplit=5)
        if len(fields) < 6 or fields[4] == b"0":  # noqa: PLR2004
            continue
        major, _, minor = fields[3].partition(b":")
        if (int(major, 16), int(minor, 16)) == (os.major(device), os.minor(device)):
            mapped.setdefault(f"mapped file {os.fsdecode(fields[5])}", None)
    return uses + list(mapped)


def _holders(proc: Path, dataset: str, mountpoint: Path) -> list[MigrationError]:
    """Return a problem for each process, in any namespace, and each mount that keeps the dataset busy or mounted."""
    device = mountpoint.stat().st_dev
    me = (proc / "self").readlink().name
    ours = str((proc / "self" / "ns" / "mnt").readlink())
    others: _Namespaces = {}
    users: list[MigrationError] = []
    unreadable: list[tuple[Path, OSError]] = []
    for pid in sorted(int(entry.name) for entry in proc.iterdir() if entry.name.isdigit() and entry.name != me):
        base = proc / str(pid)
        try:
            namespace = str((base / "ns" / "mnt").readlink())
            uses = _uses(base, device)
            if namespace != ours and namespace not in others:
                others[namespace] = (base, _mounts((base / "mountinfo").read_bytes()))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            unreadable.append((base, error))
            continue
        if uses:
            where = " from another mount namespace" if namespace != ours else ""
            more = f", and {len(uses) - 1} more" if len(uses) > 1 else ""
            users.append(MigrationError(f"{_label(base)} uses {dataset}{where}: {uses[0]}{more}", [_STOP]))
    problems = _mount_problems(proc, dataset, mountpoint, others) + users
    if unreadable:
        first, reason = unreadable[0]
        count = f"{len(unreadable)} process{'es' if len(unreadable) > 1 else ''}"
        message = f"could not inspect {count}, such as {_label(first)}: {reason.strerror}"
        problems.append(MigrationError(message, [_AS_ROOT]))
    return problems


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
    if failed:
        first, reason = min(failed, key=lambda item: item[0])
        count = f"{len(failed)} entr{'ies' if len(failed) > 1 else 'y'}"
        message = f"could not read the inode flags of {count} in {dataset}, such as {first}: {reason.strerror}"
        problems.append(MigrationError(message, [_AS_ROOT]))
    return problems


def _quota_notes(zfs: Zfs, dataset: str, mountpoint: Path) -> list[MigrationError]:
    """Return a note for the user, group and project quotas of ``dataset``, which file-move does not carry over."""
    commands = [
        f"  zfs set {shlex.quote(f'{kind}{part}quota@{name}={value}')} {dataset}"
        for kind in ("user", "group", "project")
        for name, quota, objquota in zfs.quotas(kind, dataset)
        for part, value in (("", quota), ("obj", objquota))
        if value not in {"none", "-", "0", ""}
    ]
    if not commands:
        return []
    hints = ["set them again after the swap:", *commands]
    if any(command.startswith("  zfs set project") for command in commands):
        hints.append(
            "rsync does not copy the project IDs of files either: note them first "
            f"(zfs project -r {shlex.quote(str(mountpoint))}) and set them again after the swap",
        )
    message = f"{dataset} has per-user, per-group or per-project quotas, which file-move does not carry over"
    return [MigrationError(message, hints)]


def _running(proc: Path, lock: Path) -> bool:
    """Return whether a run holds ``lock``: its device and inode on a FLOCK line of /proc/locks, not a waiter's."""
    if not lock.exists():
        return False
    st = lock.stat()
    held = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    lines = (proc / "locks").read_text().splitlines()
    return any(fields[1:2] == ["FLOCK"] and fields[5:6] == [held] for fields in map(str.split, lines))


def check_file_move(
    zfs: Zfs,
    settings: Settings,
    dataset: str,
    *,
    proc: Path = _PROC,
    lock_path: Path = LOCK_FILE,
) -> CheckReport:
    """Report what would make file-move refuse or stall, and change nothing.

    The run's own preflight gives its first refusal; then come holders, files it cannot delete and quotas it does not
    carry over. It never takes the run's lock, and while a run holds it, it reads nothing else.
    """
    if _running(proc, lock_path):
        running = MigrationError("a file-move is running; check skipped", ["check again once it has stopped"])
        return CheckReport([running], [])
    problems: list[MigrationError] = []
    try:
        preflight_file_move(zfs, settings, dataset)
    except MigrationError as refusal:
        if isinstance(refusal, ZfsError):
            raise
        problems.append(refusal)
    source = Path(zfs.get("mountpoint", dataset)) if zfs.exists(dataset) else None
    # zfs get strips the spaces around a mountpoint, so the path it gives may be another directory.
    if source is None or not source.is_absolute() or mounted_at(source) != (dataset, "zfs"):
        return CheckReport(problems, [])
    problems += _pinned_files(source, dataset) + _holders(proc, dataset, source)
    return CheckReport(problems, _quota_notes(zfs, dataset, source))
