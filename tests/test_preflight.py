"""Tests for what only file-move --check looks for: holders in every mount namespace, and inode flags."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate import preflight
from zfs_encrypt_migrate.preflight import _holders as holders
from zfs_encrypt_migrate.preflight import _pinned_files as pinned_files

from .fakeproc import CONTAINER, HOST, ROOTLESS, SELF_PID, SERVICE, FakeProc, maps_line, mount_line

if TYPE_CHECKING:
    from zfs_encrypt_migrate.errors import MigrationError

DATASET = "tank/media"
STOP = (
    "stop it, and keep whatever starts it again (a unit, a container's restart policy) disabled until file-move is done"
)
NOT_REACHED = "in another mount namespace, which file-move's unmount does not reach"
IMMUTABLE = 0x10
APPEND = 0x20


def found(problems: list[MigrationError]) -> list[tuple[str, tuple[str, ...]]]:
    """Return each problem's message with its hints."""
    return [(problem.message, problem.hints) for problem in problems]


class Host:
    """A dataset mounted at ``src`` under the pool's mount, both shared, and a fake /proc that sees them."""

    def __init__(self, tmp_path: Path, *extra: str) -> None:
        """Create the mount points; ``extra`` lines join the checking process's mountinfo."""
        self.base = Path(os.path.realpath(tmp_path))
        self.pool = self.base / "pool"
        self.src = self.pool / "media"
        (self.src / "docs").mkdir(parents=True)
        (self.src / "a.mkv").write_text("a")
        ours = (
            mount_line(1, 0, "/", "/dev/root", tags="shared:1", fstype="ext4")
            + mount_line(30, 1, self.pool, "tank", tags="shared:10")
            + mount_line(36, 30, self.src, DATASET, tags="shared:20")
            + "".join(extra)
        )
        self.proc = FakeProc(self.base / "proc", ours)

    def holders(self) -> list[MigrationError]:
        """Return what holds the dataset."""
        return holders(self.proc.root, DATASET, self.src)

    def service(self, pid: int, *extra: str, userns: str | None = None) -> None:
        """Add a process in a namespace whose mounts are slave copies of the host's, as systemd sandboxing makes."""
        mountinfo = (
            mount_line(501, 500, "/", "/dev/root", tags="master:1", fstype="ext4")
            + mount_line(530, 501, self.pool, "tank", tags="master:10")
            + mount_line(536, 530, self.src, DATASET, tags="master:20")
            + "".join(extra)
        )
        self.proc.add(pid, comm="daemon", ns=SERVICE, mountinfo=mountinfo, **({"userns": userns} if userns else {}))


@pytest.fixture
def host(tmp_path: Path) -> Host:
    """Build a host that sees only the dataset's own mount."""
    return Host(tmp_path)


def test_a_process_with_a_file_open_from_another_namespace_is_a_problem(host: Host) -> None:
    """The unmount does not see a container's open file once its bind mount is private; the check does."""
    host.proc.add(
        2345,
        comm="indexer",
        ns=CONTAINER,
        fds=[host.src / "a.mkv", "/dev/null"],
        cgroup="/system.slice/box.scope",
    )
    host.proc.add(2346, comm="idle", ns=CONTAINER, fds=["/dev/null", "socket:[1234]"])
    assert found(host.holders()) == [
        (
            (
                f"PID 2345 (indexer) in /system.slice/box.scope uses {DATASET} from another mount namespace: "
                f"open file {host.src / 'a.mkv'}"
            ),
            (STOP,),
        ),
    ]


def test_working_directory_program_and_mapped_files_count(host: Host) -> None:
    """A working directory, a program or a mapped file on the dataset keeps it busy as an open file does."""
    device = host.src.stat().st_dev
    (host.src / "lib.so").write_text("x")
    host.proc.add(3001, comm="sh", cwd=host.src / "docs")
    host.proc.add(
        3002,
        comm="player",
        exe=host.src / "a.mkv",
        maps=maps_line(Path("/usr/lib/other.so"), device + 1, 5) + maps_line(host.src / "lib.so", device, 77) * 2,
    )
    assert found(host.holders()) == [
        (f"PID 3001 (sh) uses {DATASET}: working directory {host.src / 'docs'}", (STOP,)),
        (f"PID 3002 (player) uses {DATASET}: program {host.src / 'a.mkv'}, and 1 more", (STOP,)),
    ]


def test_the_checking_process_is_not_listed(host: Host) -> None:
    """The check's own process is about to exit; its parent shell is listed if that is what holds the dataset."""
    (host.proc.root / str(SELF_PID) / "fd" / "3").symlink_to(host.src / "a.mkv")
    assert host.holders() == []


def test_a_snapshot_mounted_inside_the_dataset_is_a_problem(tmp_path: Path) -> None:
    """A snapshot automount under .zfs/snapshot makes the unmount fail with busy."""
    snap = Path(os.path.realpath(tmp_path)) / "pool" / "media" / ".zfs" / "snapshot" / "auto-1"
    host = Host(tmp_path, mount_line(40, 36, snap, f"{DATASET}@auto-1", tags="shared:25"))
    assert found(host.holders()) == [
        (
            (
                f"snapshot {DATASET}@auto-1 is mounted at {snap}, inside {DATASET}, "
                "which makes file-move's unmount fail with busy"
            ),
            (f"unmount it: umount {snap}",),
        ),
    ]


def test_a_snapshot_mounted_outside_the_dataset_blocks_its_destroy(tmp_path: Path) -> None:
    """Mounted elsewhere, a snapshot leaves the dataset's unmount alone, but zfs destroy cannot remove it."""
    elsewhere = Path(os.path.realpath(tmp_path)) / "old"
    host = Host(tmp_path, mount_line(41, 1, elsewhere, f"{DATASET}@manual"))
    assert found(host.holders()) == [
        (
            f"snapshot {DATASET}@manual is mounted at {elsewhere}, which keeps zfs destroy from removing it",
            (f"unmount it: umount {elsewhere}",),
        ),
    ]


def test_the_dataset_mounted_a_second_time_here_is_a_problem(tmp_path: Path) -> None:
    """file-move unmounts only the dataset's own mount; a second one keeps it reachable."""
    other = Path(os.path.realpath(tmp_path)) / "srv" / "media"
    host = Host(tmp_path, mount_line(37, 1, other, DATASET, tags="shared:21"))
    assert found(host.holders()) == [
        (
            f"{DATASET} is also mounted at {other}, which file-move's unmount does not reach",
            (STOP, f"or unmount it: umount {other}"),
        ),
    ]


def test_another_filesystem_mounted_inside_the_dataset_is_a_problem(tmp_path: Path) -> None:
    """A tmpfs, another dataset or a bind mount inside makes the run's unmount fail with busy."""
    root = Path(os.path.realpath(tmp_path)) / "pool" / "media"
    host = Host(
        tmp_path,
        mount_line(42, 36, root / "cache", "tmpfs", fstype="tmpfs"),
        mount_line(43, 36, root / "docs" / "other", "tank/other", tags="shared:22"),
    )
    assert found(host.holders()) == [
        (
            (
                f"tmpfs tmpfs is mounted at {root / 'cache'}, inside {DATASET}, which makes file-move's unmount fail "
                "with busy"
            ),
            (f"unmount it: umount {root / 'cache'}",),
        ),
        (
            (
                f"zfs tank/other is mounted at {root / 'docs' / 'other'}, inside {DATASET}, which makes file-move's "
                "unmount fail with busy"
            ),
            (f"unmount it: umount {root / 'docs' / 'other'}",),
        ),
    ]


def test_a_private_bind_mount_in_a_container_is_a_problem(host: Host) -> None:
    """A container's bind mount of a subdirectory keeps the dataset mounted after the unmount, so writers go unseen."""
    host.proc.add(
        2345,
        comm="indexer",
        ns=CONTAINER,
        mountinfo=mount_line(700, 650, "/", "overlay", fstype="overlay")
        + mount_line(710, 700, "/data", DATASET, root="/part"),
    )
    host.proc.add(2350, comm="helper", ns=CONTAINER)
    assert found(host.holders()) == [(f"PID 2345 (indexer) has {DATASET} mounted at /data {NOT_REACHED}", (STOP,))]


def test_copies_the_unmount_propagates_to_are_not_problems(tmp_path: Path) -> None:
    """A sandboxed service sees slave copies of the host's mounts; the unmount takes those, snapshots too, along."""
    snap = Path(os.path.realpath(tmp_path)) / "pool" / "media" / ".zfs" / "snapshot" / "auto-1"
    host = Host(tmp_path, mount_line(40, 36, snap, f"{DATASET}@auto-1", tags="shared:25"))
    host.service(5001, mount_line(540, 536, snap, f"{DATASET}@auto-1", tags="master:25"))
    assert [problem.message for problem in host.holders()] == [
        (
            f"snapshot {DATASET}@auto-1 is mounted at {snap}, inside {DATASET}, which makes file-move's unmount fail "
            "with busy"
        ),
    ]


def test_a_copy_with_other_mounts_on_it_stays_mounted(host: Host) -> None:
    """The kernel does not unmount a copy that holds mounts of its own, such as a service's TemporaryFileSystem=."""
    host.service(5001, mount_line(545, 536, host.src / "tmp", "tmpfs", fstype="tmpfs"))
    assert found(host.holders()) == [
        (
            (
                f"PID 5001 (daemon) has {DATASET} mounted at {host.src} in another mount namespace, with other mounts "
                f"on it there that keep it mounted when file-move unmounts {DATASET}"
            ),
            (STOP,),
        ),
    ]


def test_a_copy_in_another_user_namespace_is_locked(host: Host) -> None:
    """Mounts copied into a namespace of another user namespace, as in a rootless container, are never unmounted."""
    host.service(5001, userns=ROOTLESS)
    assert found(host.holders()) == [
        (
            (
                f"PID 5001 (daemon) has {DATASET} mounted at {host.src} in another user namespace, where it is locked "
                "and file-move's unmount does not reach it"
            ),
            (STOP,),
        ),
    ]


def test_a_copy_down_a_chain_may_still_be_reached(host: Host) -> None:
    """A copy of a copy is not followed down the chain: it is listed, saying the unmount may still reach it."""
    host.proc.add(
        5001,
        ns=SERVICE,
        mountinfo=mount_line(900, 899, host.pool, "tank", tags="shared:60 master:10")
        + mount_line(906, 900, host.src, DATASET, tags="shared:61 master:20"),
    )
    host.proc.add(
        5002,
        ns="mnt:[4026532700]",
        mountinfo=mount_line(950, 949, host.pool, "tank", tags="master:60")
        + mount_line(956, 950, host.src, DATASET, tags="master:61"),
    )
    assert found(host.holders()) == [
        (
            (
                f"PID 5002 (worker) has {DATASET} mounted at {host.src} in another mount namespace, copied there "
                "through another one; file-move's unmount may still reach it, but the check cannot tell"
            ),
            (STOP,),
        ),
    ]


@pytest.mark.parametrize(
    "parent_tags",
    [
        # A namespace made with unshare -m: its copies are private and get no unmount.
        "",
        # A container's root, with the dataset bound at the same path as a slave: the bind is no copy of a host mount.
        "unbindable",
    ],
)
def test_copies_the_unmount_does_not_reach_are_problems(host: Host, parent_tags: str) -> None:
    """Only a copy whose parent receives the host parent's propagation is unmounted with it."""
    host.proc.add(
        6001,
        comm="shell",
        ns=CONTAINER,
        mountinfo=mount_line(960, 959, host.pool, "tank", tags=parent_tags)
        + mount_line(966, 960, host.src, DATASET, tags="master:20"),
    )
    assert [problem.message for problem in host.holders()] == [
        f"PID 6001 (shell) has {DATASET} mounted at {host.src} {NOT_REACHED}",
    ]


def test_a_snapshot_mounted_only_in_another_namespace_is_a_problem(host: Host) -> None:
    """There it never expires, and zfs destroy cannot remove the snapshot while it is mounted."""
    host.proc.add(
        2345,
        comm="indexer",
        ns=CONTAINER,
        mountinfo=mount_line(710, 700, "/data", DATASET)
        + mount_line(720, 710, "/data/.zfs/snapshot/auto-2", f"{DATASET}@auto-2"),
    )
    assert found(host.holders())[1] == (
        (
            f"PID 2345 (indexer) has snapshot {DATASET}@auto-2 mounted at /data/.zfs/snapshot/auto-2 in another mount "
            "namespace, which keeps zfs destroy from removing it"
        ),
        (STOP,),
    )


def test_paths_with_a_carriage_return_stay_whole(host: Host) -> None:
    """The kernel escapes a newline in mountinfo but not a carriage return, and maps escapes neither."""
    mapped = host.src / "x\ry"
    mapped.write_text("x")
    host.proc.add(
        2345,
        ns=CONTAINER,
        mountinfo=mount_line(710, 700, "/da\rta", DATASET),
        maps=maps_line(mapped, host.src.stat().st_dev, 77),
    )
    assert [problem.message for problem in host.holders()] == [
        f"PID 2345 (worker) has {DATASET} mounted at /da\rta {NOT_REACHED}",
        f"PID 2345 (worker) uses {DATASET} from another mount namespace: mapped file {mapped}",
    ]


def test_other_datasets_and_filesystems_are_ignored(host: Host) -> None:
    """Only mounts of the dataset and its snapshots count, whatever else is mounted where."""
    host.proc.add(
        2345,
        ns=CONTAINER,
        mountinfo=mount_line(710, 700, "/data", "tank/music")
        + mount_line(711, 700, "/media2", "tank/media2")
        + mount_line(712, 700, "/x", DATASET, fstype="tmpfs"),
    )
    assert host.holders() == []


def test_the_namespace_is_named_by_its_first_process(host: Host) -> None:
    """Processes are read in PID order, so the hint names the same process on every run."""
    info = mount_line(710, 700, "/data", DATASET)
    for pid in (2400, 2345, 2390):
        host.proc.add(pid, comm=f"p{pid}", ns=CONTAINER, mountinfo=info)
    assert host.holders()[0].message.startswith("PID 2345 (p2345) has")


def test_the_host_namespace_of_another_process_is_not_read_again(host: Host) -> None:
    """Processes in the checking process's own namespace see the same mounts; only their files count."""
    host.proc.add(3001, ns=HOST, mountinfo=mount_line(37, 1, "/elsewhere", DATASET))
    assert host.holders() == []


# --- inode flags ---


def flagged_tree(tmp_path: Path) -> Path:
    """Make a dataset root with files, directories, a symlink, a fifo and a top-level .zfs."""
    root = tmp_path / "media"
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    (root / "my file").write_text("m")
    (root / "docs" / "b.txt").write_text("b")
    (root / "link").symlink_to("a.txt")
    os.mkfifo(root / "fifo")
    (root / ".zfs" / "snapshot").mkdir(parents=True)
    (root / ".zfs" / "snapshot" / "x").write_text("x")
    return root


def test_immutable_and_append_only_entries_are_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run would stop at the first one it cannot delete; the check lists them all, with the chattr commands."""
    root = flagged_tree(tmp_path)
    flags = {
        root / "a.txt": IMMUTABLE,
        root / "my file": IMMUTABLE | APPEND,
        root / "docs": APPEND,
        root / ".zfs" / "snapshot" / "x": IMMUTABLE,
    }
    read: list[str] = []

    def fake(path: str) -> int:
        read.append(path)
        # Other flags, such as the extent flag of ext4, do not stop a delete.
        return flags.get(Path(path), 0) | 0x80000

    monkeypatch.setattr(preflight, "_inode_flags", fake)
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
    # Regular files and directories only, as lsattr reads them: opening a fifo would block, a symlink has no flags.
    assert sorted(read) == sorted(
        str(root / name) for name in ("", "a.txt", "my file", "docs", "docs/deep", "docs/b.txt")
    )


def test_no_flags_no_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tree without the flags passes."""
    monkeypatch.setattr(preflight, "_inode_flags", lambda _path: 0)
    assert pinned_files(flagged_tree(tmp_path), DATASET) == []


def test_flags_that_cannot_be_read_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry whose flags stay unknown may be one the run cannot delete."""
    root = flagged_tree(tmp_path)

    def fake(path: str) -> int:
        if path.endswith("b.txt"):
            raise OSError(errno.EIO, "Input/output error", path)
        if path.endswith("a.txt"):
            # Deleted since the directory was listed: nothing left to check.
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        return 0

    monkeypatch.setattr(preflight, "_inode_flags", fake)
    assert found(pinned_files(root, DATASET)) == [
        (
            (
                f"could not read the inode flags of 1 entry in {DATASET}, such as {root / 'docs' / 'b.txt'}: "
                "Input/output error"
            ),
            ("check them by hand with lsattr -d; the run stops at an immutable or append-only one",),
        ),
    ]


def test_a_leased_file_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An NFS delegation or a Samba oplock refuses a non-blocking open; that is not a matter of permissions."""
    root = flagged_tree(tmp_path)

    def fake(path: str) -> int:
        if path.endswith("a.txt"):
            raise BlockingIOError(errno.EWOULDBLOCK, "Resource temporarily unavailable", path)
        return 0

    monkeypatch.setattr(preflight, "_inode_flags", fake)
    assert found(pinned_files(root, DATASET)) == [
        (
            (
                f"{DATASET} has leased files, such as {root / 'a.txt'}: an NFS client or Samba holds a delegation or "
                "oplock on them"
            ),
            ("stop the NFS server and the Samba exports of the dataset; the run would wait on each leased file",),
        ),
    ]


def test_another_filesystem_inside_is_reported_not_walked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flags walk stays on the dataset; the filesystem mounted inside is a problem of its own."""
    root = Path(os.path.realpath(tmp_path)) / "pool" / "media"
    host = Host(tmp_path, mount_line(42, 36, root / "docs", "tmpfs", fstype="tmpfs"))
    (host.src / "docs" / "inner.txt").write_text("i")
    real = os.lstat

    def lstat(path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result:
        st = real(path, *args, **kwargs)  # type: ignore[arg-type]
        if Path(path).name == "docs":
            values = list(st)
            values[2] = st.st_dev + 1
            return os.stat_result(values)
        return st

    read: list[str] = []
    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(preflight, "_inode_flags", lambda path: read.append(path) or 0)
    assert pinned_files(host.src, DATASET) == []
    assert not [path for path in read if "docs" in path]
    assert [problem.message for problem in host.holders()] == [
        f"tmpfs tmpfs is mounted at {root / 'docs'}, inside {DATASET}, which makes file-move's unmount fail with busy",
    ]


def test_reads_the_flags_with_the_ioctl(tmp_path: Path) -> None:
    """FS_IOC_GETFLAGS works without lsattr; a fresh file and directory have neither flag."""
    path = tmp_path / "plain"
    path.write_text("x")
    try:
        flags = preflight._inode_flags(str(path))  # noqa: SLF001
    except OSError as error:
        if error.errno in {errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL}:
            pytest.skip(f"this filesystem has no inode flags: {error}")
        raise
    assert flags & (IMMUTABLE | APPEND) == 0
    assert preflight._inode_flags(str(tmp_path)) & (IMMUTABLE | APPEND) == 0  # noqa: SLF001
