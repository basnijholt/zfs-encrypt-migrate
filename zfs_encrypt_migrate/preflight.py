"""Read-only checks that only ``file-move --check`` runs.

They find what the run would trip over only once it is under way: whatever keeps the dataset in use or mounted, in
any mount namespace; files it cannot delete; and quotas it does not carry over.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .errors import MigrationError, ZfsError
from .filetree import mountinfo_field

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .zfs import Zfs

PROC = Path("/proc")
_AS_ROOT = "run the check as root, like file-move itself"
_STOP = (
    "stop it, and keep whatever starts it again (a unit, a container's restart policy) disabled until file-move is done"
)
# What a process can hold a filesystem by, besides its open and mapped files.
_LINKS = (("cwd", "working directory"), ("root", "root directory"), ("exe", "program"))
_IMMUTABLE = 0x10  # FS_IMMUTABLE_FL
_APPEND = 0x20  # FS_APPEND_FL
# FS_IOC_GETFLAGS is _IOR('f', 1, long) in the generic ioctl encoding, which x86 and arm use.
_GETFLAGS = (2 << 30) | (struct.calcsize("l") << 16) | (ord("f") << 8) | 1
_UNSET = {"none", "-", "0", ""}


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _label(base: Path) -> str:
    """Name the process whose /proc directory is ``base``: its PID, command and, when known, cgroup."""
    label = f"PID {base.name}"
    comm = _text(base / "comm")
    if comm is not None:
        label += f" ({comm.strip()})"
    lines = (_text(base / "cgroup") or "").splitlines()
    cgroup = next((line.removeprefix("0::") for line in lines if line.startswith("0::")), "/")
    return label if cgroup == "/" else f"{label} in {cgroup}"


@dataclass(frozen=True)
class _Mount:
    """One line of a mountinfo file; ``shared`` and ``master`` are its peer group and the one it is a slave of."""

    mount_id: int
    parent: int
    root: str
    point: str
    shared: int | None
    master: int | None
    fstype: str
    source: str


def _mounts(raw: bytes) -> list[_Mount]:
    """Parse mountinfo: ID, parent ID, device, root, mount point, options, optional fields, "-", type, source."""
    mounts = []
    for line in raw.splitlines():
        fields = line.split(b" ")
        separator = fields.index(b"-", 6)
        tags: dict[bytes, bytes] = {}
        for field in fields[6:separator]:
            name, _, value = field.partition(b":")
            tags[name] = value
        mounts.append(
            _Mount(
                mount_id=int(fields[0]),
                parent=int(fields[1]),
                root=mountinfo_field(fields[3]),
                point=mountinfo_field(fields[4]),
                shared=int(tags[b"shared"]) if b"shared" in tags else None,
                master=int(tags[b"master"]) if b"master" in tags else None,
                fstype=mountinfo_field(fields[separator + 1]),
                source=mountinfo_field(fields[separator + 2]),
            ),
        )
    return mounts


def _reach(group: int, mounts: Iterable[_Mount]) -> set[int]:
    """Return the peer groups that get the mounts and unmounts of ``group``: itself and its slaves, down any chain."""
    slaves = [(mount.master, mount.shared) for mount in mounts if mount.master is not None and mount.shared is not None]
    reach = {group}
    grew = True
    while grew:
        grew = False
        for master, shared in slaves:
            if master in reach and shared not in reach:
                reach.add(shared)
                grew = True
    return reach


def _place(mount: _Mount, parent: _Mount) -> str | None:
    """Return where in its parent's filesystem ``mount`` sits."""
    try:
        return str(PurePosixPath(parent.root) / PurePosixPath(mount.point).relative_to(parent.point))
    except ValueError:
        return None


def _propagated(origin: _Mount, copy: _Mount, mounts: Mapping[int, _Mount]) -> bool:
    """Return whether unmounting ``origin`` unmounts ``copy`` too.

    The kernel passes an unmount on to the peers and slaves of the parent mount, and unmounts there what sits at the
    same place. A bind mount elsewhere, of a subdirectory, or in a namespace whose copies are private gets nothing.
    """
    parent, copy_parent = mounts.get(origin.parent), mounts.get(copy.parent)
    if parent is None or copy_parent is None or parent.shared is None or origin.root != copy.root:
        return False
    place = _place(origin, parent)
    if place is None or place != _place(copy, copy_parent):
        return False
    reach = _reach(parent.shared, mounts.values())
    return copy_parent.shared in reach or copy_parent.master in reach


def _of(mount: _Mount, dataset: str) -> bool:
    """Return whether ``mount`` is of ``dataset`` or of a snapshot of it."""
    return mount.fstype == "zfs" and (mount.source == dataset or mount.source.startswith(f"{dataset}@"))


def _here(mount: _Mount, dataset: str) -> MigrationError:
    point = shlex.quote(mount.point)
    if mount.source == dataset:
        return MigrationError(
            f"{dataset} is also mounted at {mount.point}, which file-move's unmount does not reach",
            [f"stop what uses it, then unmount it: umount {point}"],
        )
    return MigrationError(
        f"snapshot {mount.source} is mounted at {mount.point}, which keeps {dataset} busy",
        [f"unmount it: umount {point}"],
    )


def _there(base: Path, mount: _Mount, dataset: str) -> MigrationError:
    unmount = f"nsenter -t {base.name} -m umount {shlex.quote(mount.point)}"
    if mount.source == dataset:
        return MigrationError(
            f"{_label(base)} has {dataset} mounted at {mount.point} in another mount namespace, which file-move's "
            "unmount does not reach",
            [_STOP, f"or unmount it there: {unmount}"],
        )
    return MigrationError(
        f"snapshot {mount.source} is mounted at {mount.point} in the mount namespace of {_label(base)}, where it does "
        f"not expire and keeps {dataset} busy",
        [f"unmount it, before you destroy the snapshot: {unmount}"],
    )


def _mount_problems(
    proc: Path,
    dataset: str,
    mountpoint: Path,
    others: Mapping[str, tuple[Path, list[_Mount]]],
) -> list[MigrationError]:
    """Return a problem for each mount of the dataset or its snapshots that file-move's unmount leaves in place.

    ``others`` gives, for each other mount namespace, the /proc directory of its first process and its mounts.
    """
    ours = _mounts((proc / "self" / "mountinfo").read_bytes())
    # Mount IDs are unique across namespaces.
    every = {mount.mount_id: mount for mount in ours} | {
        mount.mount_id: mount for _base, mounts in others.values() for mount in mounts
    }
    target = os.path.realpath(mountpoint)
    mine = [mount for mount in ours if _of(mount, dataset)]
    # Of several mounts on one path, the one listed last covers the others.
    own = next((mount for mount in reversed(mine) if mount.source == dataset and mount.point == target), None)
    problems = [_here(mount, dataset) for mount in mine if mount is not own]
    for base, mounts in others.values():
        problems += [
            _there(base, mount, dataset)
            for mount in mounts
            if _of(mount, dataset)
            and not any(origin.source == mount.source and _propagated(origin, mount, every) for origin in mine)
        ]
    return problems


def _on(link: Path, device: int) -> str | None:
    """Return where ``link`` points when that is on ``device``; None when not, or when it is gone."""
    try:
        if link.stat().st_dev != device:
            return None
        return str(link.readlink())
    except (FileNotFoundError, ProcessLookupError):
        return None


def _mapped(maps: Path, device: int) -> list[str]:
    """Return the files a process maps from ``device``, each once."""
    try:
        raw = maps.read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return []
    found: dict[str, None] = {}
    for line in raw.splitlines():
        # Address, permissions, offset, device, inode, path.
        fields = line.split(maxsplit=5)
        if len(fields) < 6 or fields[4] == b"0":  # noqa: PLR2004
            continue
        major, _, minor = fields[3].partition(b":")
        if (int(major, 16), int(minor, 16)) == (os.major(device), os.minor(device)):
            found.setdefault(os.fsdecode(fields[5]), None)
    return list(found)


def _uses(base: Path, device: int) -> list[str]:
    """Describe what of the process at ``base`` is on ``device``: its directories, program, open and mapped files."""
    uses = [f"{what} {target}" for name, what in _LINKS if (target := _on(base / name, device))]
    try:
        fds = sorted((base / "fd").iterdir(), key=lambda fd: int(fd.name))
    except (FileNotFoundError, ProcessLookupError):
        fds = []
    uses += [f"open file {target}" for fd in fds if (target := _on(fd, device))]
    uses += [f"mapped file {path}" for path in _mapped(base / "maps", device)]
    return uses


def _processes(proc: Path) -> tuple[str, list[tuple[Path, str]], list[tuple[Path, OSError]]]:
    """Return this process's mount namespace; every other process, in PID order, with its own; and those unreadable."""
    me = (proc / "self").readlink().name
    ours = str((proc / "self" / "ns" / "mnt").readlink())
    found: list[tuple[Path, str]] = []
    unreadable: list[tuple[Path, OSError]] = []
    for pid in sorted(int(entry.name) for entry in proc.iterdir() if entry.name.isdigit() and entry.name != me):
        base = proc / str(pid)
        try:
            found.append((base, str((base / "ns" / "mnt").readlink())))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            unreadable.append((base, error))
    return ours, found, unreadable


def holders(proc: Path, dataset: str, mountpoint: Path) -> list[MigrationError]:
    """Return a problem for each thing that keeps ``dataset`` in use or mounted once file-move unmounts it.

    That is each mount of it or of a snapshot of it that the unmount does not take along: one at another path here, a
    snapshot mounted under ``.zfs``, or one in another mount namespace that is no propagated copy of a mount here,
    such as a container's bind mount. And each process with a file, directory, program or mapping on it, in any
    namespace. ``proc`` is where procfs is mounted, ``mountpoint`` the dataset's own mount here. A process that cannot
    be read, without root, is a problem too: what it holds stays unknown.
    """
    device = mountpoint.stat().st_dev
    ours, processes, unreadable = _processes(proc)
    others: dict[str, tuple[Path, list[_Mount]]] = {}
    users: list[MigrationError] = []
    for base, namespace in processes:
        try:
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
        count = _plural(len(unreadable), "process", "processes")
        problems.append(
            MigrationError(f"could not inspect {count}, such as {_label(first)}: {reason.strerror}", [_AS_ROOT]),
        )
    return problems


def _inode_flags(path: str) -> int:
    """Return the inode flags of a regular file or directory, as lsattr reads them, without following a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        result = fcntl.ioctl(fd, _GETFLAGS, bytes(struct.calcsize("l")))
    finally:
        os.close(fd)
    # The kernel writes an int, though the request is declared with a long.
    flags: int = struct.unpack_from("I", result)[0]
    return flags


def _listing(directory: str, *, top: bool, device: int) -> tuple[list[str], list[str]]:
    """Return the subdirectories on ``device`` and the regular files in ``directory``, skipping a top-level .zfs."""
    with os.scandir(directory) as it:
        children = list(it)
    subdirectories: list[str] = []
    files: list[str] = []
    for child in children:
        if top and child.name == ".zfs":
            continue
        try:
            if child.is_dir(follow_symlinks=False):
                if os.lstat(child.path).st_dev == device:
                    subdirectories.append(child.path)
            elif child.is_file(follow_symlinks=False):
                files.append(child.path)
        except FileNotFoundError:
            continue
    return subdirectories, files


def _flagged(root: Path) -> tuple[list[tuple[str, int]], list[tuple[str, OSError]]]:
    """Return the immutable or append-only regular files and directories under ``root``, and those unreadable.

    Like the scan of a batch it never follows a symlink, skips a top-level ``.zfs`` and stays on the filesystem of
    ``root``. Other files have no flags lsattr reads, and opening a device or a fifo could block or act on it.
    """
    device = os.lstat(root).st_dev
    flagged: list[tuple[str, int]] = []
    failed: list[tuple[str, OSError]] = []
    pending = [str(root)]
    while pending:
        directory = pending.pop()
        try:
            subdirectories, files = _listing(directory, top=directory == str(root), device=device)
        except FileNotFoundError:
            continue
        except OSError as error:
            failed.append((directory, error))
            subdirectories, files = [], []
        pending += subdirectories
        for path in (directory, *files):
            try:
                flags = _inode_flags(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                failed.append((path, error))
                continue
            if flags & (_IMMUTABLE | _APPEND):
                flagged.append((path, flags))
    return sorted(flagged), sorted(failed, key=lambda item: item[0])


def pinned_files(root: Path, dataset: str) -> list[MigrationError]:
    """Return a problem for the files and directories under ``root`` that file-move cannot delete, or delete from.

    Neither an immutable or append-only file nor any name in such a directory can be unlinked, so the run would stop
    at the first one. ``root`` is the mountpoint of ``dataset``.
    """
    flagged, failed = _flagged(root)
    problems = []
    if flagged:
        commands = []
        for path, flags in flagged:
            letters = "".join(letter for bit, letter in ((_IMMUTABLE, "i"), (_APPEND, "a")) if flags & bit)
            quoted = shlex.quote(path)
            commands.append(f"  chattr -{letters} {quoted}  # after the swap: chattr +{letters} {quoted}")
        problems.append(
            MigrationError(
                f"{dataset} has files or directories with the immutable or append-only flag, which keeps file-move "
                "from deleting them or the files in them",
                ["clear the flags; the copies get none, so set them again after the swap:", *commands],
            ),
        )
    if failed:
        path, error = failed[0]
        count = _plural(len(failed), "entry", "entries")
        problems.append(
            MigrationError(
                f"could not read the inode flags of {count} in {dataset}, such as {path}: {error.strerror}",
                [_AS_ROOT],
            ),
        )
    return problems


def quota_notes(zfs: Zfs, dataset: str, mountpoint: Path) -> list[MigrationError]:
    """Return a note for the user, group and project quotas of ``dataset``, which file-move does not carry over.

    ``zfs get all`` does not list them, so the copy never gets them; the note gives the commands that set them again.
    """
    notes: list[MigrationError] = []
    commands: list[str] = []
    projects = False
    for kind in ("user", "group", "project"):
        try:
            rows = zfs.quotas(kind, dataset)
        except ZfsError as error:
            notes.append(
                MigrationError(
                    f"could not list the {kind} quotas of {dataset}: {error.message}",
                    [f"check them by hand: zfs {kind}space {dataset}"],
                ),
            )
            continue
        for name, quota, objquota in rows:
            for prop, value in ((f"{kind}quota", quota), (f"{kind}objquota", objquota)):
                if value not in _UNSET:
                    commands.append(f"  zfs set {shlex.quote(f'{prop}@{name}={value}')} {dataset}")
                    projects = projects or kind == "project"
    if commands:
        hints = ["set them again after the swap:", *commands]
        if projects:
            hints.append(
                "rsync does not copy the project IDs of files either: note them first "
                f"(zfs project -r {shlex.quote(str(mountpoint))}) and set them again after the swap",
            )
        notes.append(
            MigrationError(
                f"{dataset} has per-user, per-group or per-project quotas, which file-move does not carry over",
                hints,
            ),
        )
    return notes
