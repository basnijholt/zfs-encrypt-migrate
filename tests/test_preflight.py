"""Tests for what only file-move --check looks for: snapshot mounts, mounts inside the dataset, inode flags."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate import preflight
from zfs_encrypt_migrate.preflight import _pinned_files as pinned_files
from zfs_encrypt_migrate.preflight import _snapshot_mounts as snapshot_mounts
from zfs_encrypt_migrate.preflight import _submounts as submounts

from .fakeproc import SELF_PID, FakeProc, mount_line

if TYPE_CHECKING:
    from zfs_encrypt_migrate.errors import MigrationError

DATASET = "tank/media"
IMMUTABLE = 0x10
APPEND = 0x20
ELSEWHERE = "stop the processes or containers that hold it: a mounted snapshot keeps zfs destroy from removing it"


def found(problems: list[MigrationError]) -> list[tuple[str, tuple[str, ...]]]:
    """Return each problem's message with its hints."""
    return [(problem.message, problem.hints) for problem in problems]


class Host:
    """A dataset mounted at ``src`` under the pool's mount, and a fake /proc whose checking process sees them."""

    def __init__(self, tmp_path: Path, *extra: str) -> None:
        """Create the mount points; ``extra`` lines join the checking process's mountinfo."""
        self.base = Path(os.path.realpath(tmp_path))
        self.src = self.base / "pool" / "media"
        (self.src / "docs").mkdir(parents=True)
        self.snap = self.src / ".zfs" / "snapshot" / "auto-1"
        ours = (
            mount_line(1, 0, "/", "/dev/root", fstype="ext4")
            + mount_line(30, 1, self.base / "pool", "tank")
            + mount_line(36, 30, self.src, DATASET)
            + "".join(extra)
        )
        self.proc = FakeProc(self.base / "proc", ours)

    def snapshots(self) -> list[MigrationError]:
        """Return the snapshot mounts found."""
        return snapshot_mounts(self.proc.root, DATASET)


@pytest.fixture
def host(tmp_path: Path) -> Host:
    """Build a host that sees only the dataset's own mount."""
    return Host(tmp_path)


def automount(host: Host, mount_id: int = 40, snap: str = "auto-1") -> str:
    """Return the mountinfo line of a snapshot automounted under the dataset."""
    return mount_line(mount_id, 36, host.src / ".zfs" / "snapshot" / snap, f"{DATASET}@{snap}")


# --- snapshot mounts ---


def test_a_snapshot_mounted_here_is_a_problem(tmp_path: Path) -> None:
    """A snapshot automount under .zfs/snapshot makes the unmount fail with busy and keeps zfs destroy off it."""
    snap = Path(os.path.realpath(tmp_path)) / "pool" / "media" / ".zfs" / "snapshot" / "auto-1"
    host = Host(tmp_path, mount_line(40, 36, snap, f"{DATASET}@auto-1"))
    assert found(host.snapshots()) == [
        (f"snapshot {DATASET}@auto-1 is mounted at {snap}", (f"unmount it: umount {snap}",)),
    ]


def test_a_snapshot_mounted_only_in_another_namespace_is_a_problem(host: Host) -> None:
    """In a container's namespace it never expires; the check finds it by its name, wherever it is mounted."""
    host.proc.add(
        2345,
        comm=b"indexer",
        mountinfo=mount_line(720, 710, "/data/.zfs/snapshot/auto-2", f"{DATASET}@auto-2"),
    )
    assert found(host.snapshots()) == [
        (
            f"snapshot {DATASET}@auto-2 is mounted at /data/.zfs/snapshot/auto-2, as seen by PID 2345 (indexer)",
            (ELSEWHERE,),
        ),
    ]


def test_a_snapshot_only_one_thread_sees_is_found(host: Host) -> None:
    """Mountinfo leaves out mounts outside a thread's root, as in a chroot, so every thread's is read."""
    host.proc.add(2345, mountinfo="", threads={2346: mount_line(720, 710, "/jail/snap", f"{DATASET}@auto-2")})
    assert [problem.message for problem in host.snapshots()] == [
        f"snapshot {DATASET}@auto-2 is mounted at /jail/snap, as seen by PID 2345 (worker)",
    ]


def test_each_snapshot_mount_is_reported_once(host: Host) -> None:
    """Many processes see the same mount; it is one finding, with the fix that works from here when it can."""
    for pid in (2345, 2346, SELF_PID + 1):
        host.proc.add(pid, mountinfo=automount(host))
    (host.proc.root / str(SELF_PID) / "task" / str(SELF_PID) / "mountinfo").write_text(automount(host))
    snap = host.src / ".zfs" / "snapshot" / "auto-1"
    assert found(host.snapshots()) == [
        (f"snapshot {DATASET}@auto-1 is mounted at {snap}", (f"unmount it: umount {snap}",)),
    ]


def test_other_snapshots_and_filesystems_are_ignored(host: Host) -> None:
    """Only snapshots of the dataset count: not of a dataset whose name starts alike, nor other filesystems."""
    host.proc.add(
        2345,
        mountinfo=mount_line(710, 700, "/a", "tank/media2@x")
        + mount_line(711, 700, "/b", "tank/music@x")
        + mount_line(712, 700, "/c", f"{DATASET}@x", fstype="tmpfs")
        + mount_line(713, 700, "/d", DATASET),
    )
    assert host.snapshots() == []


def test_unreadable_mount_tables_are_reported_and_vanished_ones_skipped(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task that exits meanwhile has nothing to hide; one that cannot be read leaves the scan incomplete."""
    stuck = host.proc.add(77, comm=b"agent")
    gone = host.proc.add(78)
    (gone / "task" / "78" / "mountinfo").unlink()
    real = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == stuck / "task" / "77" / "mountinfo":
            raise OSError(errno.ENOTCONN, "Transport endpoint is not connected", str(path))
        return real(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert found(host.snapshots()) == [
        (
            "could not read the mounts of 1 process, such as PID 77 (agent): Transport endpoint is not connected",
            ("check again; until then, snapshots mounted there are unknown",),
        ),
    ]


def test_odd_bytes_in_names_neither_crash_nor_split_the_report(host: Host) -> None:
    """A carriage return stays in its path, and a byte that is no UTF-8 shows as an escape."""
    host.proc.add(
        2345,
        comm=b"bad\xff",
        mountinfo=mount_line(720, 710, "/da\rta", f"{DATASET}@a") + mount_line(721, 710, "/x\udcff", f"{DATASET}@b"),
    )
    assert [problem.message for problem in host.snapshots()] == [
        f"snapshot {DATASET}@a is mounted at /da\rta, as seen by PID 2345 (bad\\xff)",
        f"snapshot {DATASET}@b is mounted at /x\\xff, as seen by PID 2345 (bad\\xff)",
    ]


# --- mounts inside the dataset ---


def test_another_filesystem_mounted_inside_the_dataset_is_a_problem(host: Host) -> None:
    """A tmpfs, another dataset or a bind mount on the dataset makes the run's unmount fail with busy."""
    lines = (
        mount_line(42, 36, host.src / "cache", "tmpfs", fstype="tmpfs")
        + mount_line(43, 36, host.src / "docs" / "other", "tank/other")
        + automount(host)
        + mount_line(44, 1, host.base / "elsewhere", "tmpfs", fstype="tmpfs")
    )
    (host.proc.root / str(SELF_PID) / "mountinfo").write_text(
        (host.proc.root / "self" / "mountinfo").read_text() + lines,
    )
    busy = f"inside {DATASET}, which makes the run's unmount fail with busy"
    assert found(submounts(host.proc.root, DATASET, host.src)) == [
        (f"tmpfs tmpfs is mounted at {host.src / 'cache'}, {busy}", (f"unmount it: umount {host.src / 'cache'}",)),
        (
            f"zfs tank/other is mounted at {host.src / 'docs' / 'other'}, {busy}",
            (f"unmount it: umount {host.src / 'docs' / 'other'}",),
        ),
    ]


# --- inode flags ---


def flagged_tree(tmp_path: Path) -> Path:
    """Make a dataset root with files, directories, a symlink, a fifo and a top-level .zfs."""
    root = tmp_path / "media"
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    (root / "my file").write_text("m")
    (root / "docs" / "b.txt").write_text("b")
    (root / "docs" / "deep" / "c.txt").write_text("c")
    (root / "link").symlink_to("a.txt")
    os.mkfifo(root / "fifo")
    (root / ".zfs" / "snapshot").mkdir(parents=True)
    (root / ".zfs" / "snapshot" / "x").write_text("x")
    return root


def flags_by_name(monkeypatch: pytest.MonkeyPatch, root: Path, flags: dict[str, int | OSError]) -> list[str]:
    """Answer the flags of the entries named in ``flags`` (relative to ``root``); return the names read."""
    names = {os.lstat(root / name).st_ino: name for name in ["", *flags]}
    names |= {os.lstat(path).st_ino: str(path.relative_to(root)) for path in root.rglob("*") if not path.is_symlink()}
    read: list[str] = []

    def fake(fd: int) -> int:
        name = names[os.fstat(fd).st_ino]
        read.append(name)
        answer = flags.get(name, 0)
        if isinstance(answer, OSError):
            raise answer
        # Other flags, such as the extent flag of ext4, do not stop a delete.
        return answer | 0x80000

    monkeypatch.setattr(preflight, "_inode_flags", fake)
    return read


def test_immutable_and_append_only_entries_are_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run would stop at the first one it cannot delete; the check lists them all, with the chattr commands."""
    root = flagged_tree(tmp_path)
    read = flags_by_name(
        monkeypatch,
        root,
        {"a.txt": IMMUTABLE, "my file": IMMUTABLE | APPEND, "docs": APPEND, ".zfs/snapshot/x": IMMUTABLE},
    )
    a, mine, docs = root / "a.txt", "'" + str(root / "my file") + "'", root / "docs"
    assert found(pinned_files(root, DATASET)) == [
        (
            (
                f"{DATASET} has files or directories with the immutable or append-only flag, "
                "which keeps file-move from deleting them or the files in them"
            ),
            (
                "clear the flags; the copies get none, so set them again after the swap:",
                f"  chattr -i {a}  # after the swap: chattr +i {a}",
                f"  chattr -a {docs}  # after the swap: chattr +a {docs}",
                f"  chattr -ia {mine}  # after the swap: chattr +ia {mine}",
            ),
        ),
    ]
    # Regular files and directories only, as lsattr reads them: opening a fifo could block, a symlink has no flags.
    assert sorted(read) == sorted(["", "a.txt", "my file", "docs", "docs/deep", "docs/b.txt", "docs/deep/c.txt"])


def test_no_flags_no_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tree without the flags passes."""
    root = flagged_tree(tmp_path)
    flags_by_name(monkeypatch, root, {})
    assert pinned_files(root, DATASET) == []


def test_unreadable_entries_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry whose flags stay unknown may be one the run cannot delete; one that vanished is no concern."""
    root = flagged_tree(tmp_path)
    error = BlockingIOError(errno.EWOULDBLOCK, "Resource temporarily unavailable")
    flags_by_name(monkeypatch, root, {"docs/b.txt": error, "a.txt": FileNotFoundError(errno.ENOENT, "gone")})
    assert found(pinned_files(root, DATASET)) == [
        (
            (
                f"could not read the inode flags of 1 entry in {DATASET}, such as {root / 'docs' / 'b.txt'}: "
                "Resource temporarily unavailable"
            ),
            ("check them by hand with lsattr -d; the run stops at an immutable or append-only one",),
        ),
    ]


def test_each_directory_is_entered_once_and_never_another_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bind mount of the dataset inside itself would loop; another filesystem inside is not the dataset's."""
    root = flagged_tree(tmp_path)
    (root / "other").mkdir()
    (root / "other" / "o.txt").write_text("o")
    docs, deep, other = (os.lstat(root / name) for name in ("docs", "docs/deep", "other"))
    real = os.fstat

    def fstat(fd: int) -> os.stat_result:
        st = real(fd)
        values = list(st)
        if st.st_ino == deep.st_ino:
            values[1] = docs.st_ino  # docs/deep shows as docs again, as through a bind mount
        if st.st_ino == other.st_ino:
            values[2] = st.st_dev + 1
        return os.stat_result(values)

    read = flags_by_name(monkeypatch, root, {})
    monkeypatch.setattr(os, "fstat", fstat)
    assert pinned_files(root, DATASET) == []
    assert not [name for name in read if name.startswith(("docs/deep", "other"))]


@pytest.mark.parametrize(("code", "retried"), [(errno.EPERM, True), (errno.EACCES, False)])
def test_noatime_is_dropped_only_where_the_kernel_refuses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    retried: bool,  # noqa: FBT001
) -> None:
    """O_NOATIME keeps the reads from writing access times; only EPERM, not owning the file, opens without it."""
    root = flagged_tree(tmp_path)
    flags_by_name(monkeypatch, root, {})
    real = os.open

    def open_(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if flags & os.O_NOATIME and path.endswith("a.txt"):
            raise PermissionError(code, os.strerror(code), path)
        return real(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", open_)
    messages = [problem.message for problem in pinned_files(root, DATASET)]
    assert messages == (
        []
        if retried
        else [f"could not read the inode flags of 1 entry in {DATASET}, such as {root / 'a.txt'}: Permission denied"]
    )


def test_names_that_are_no_utf8_show_as_escapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file name with a byte that is no UTF-8 neither crashes the report nor stops it halfway."""
    root = flagged_tree(tmp_path)
    bad = root / os.fsdecode(b"bad\xff")
    bad.write_text("x")
    flags_by_name(monkeypatch, root, {os.fsdecode(b"bad\xff"): IMMUTABLE})
    shown = str(root) + "/bad\\xff"
    assert pinned_files(root, DATASET)[0].hints[1] == f"  chattr -i '{shown}'  # after the swap: chattr +i '{shown}'"


def test_reads_the_flags_with_the_ioctl(tmp_path: Path) -> None:
    """FS_IOC_GETFLAGS works without lsattr; a fresh file and directory have neither flag."""
    path = tmp_path / "plain"
    path.write_text("x")
    for entry in (path, tmp_path):
        fd = os.open(entry, os.O_RDONLY)
        try:
            flags = preflight._inode_flags(fd)  # noqa: SLF001
        except OSError as error:
            if error.errno in {errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL}:
                pytest.skip(f"this filesystem has no inode flags: {error}")
            raise
        finally:
            os.close(fd)
        assert flags & (IMMUTABLE | APPEND) == 0
