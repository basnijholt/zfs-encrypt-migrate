"""Tests for the filesystem layer of file-move."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from zfs_encrypt_migrate import filetree
from zfs_encrypt_migrate.constants import SCRATCH
from zfs_encrypt_migrate.errors import MigrationError
from zfs_encrypt_migrate.filetree import (
    Group,
    _Entry,
    delete,
    device_of,
    group_by_inode,
    mount_device,
    mounted_at,
    plan_batches,
    remove_scratch,
    scan,
    verify,
    write_file_list,
)

BAD_NAME = os.fsdecode(b"bad\xff")


def entry(name: str, ino: int = 1, nlink: int = 1, allocated: int = 0) -> _Entry:
    """Entry."""
    return _Entry(name=name, ino=ino, nlink=nlink, ctime_ns=0, allocated=allocated)


def group(name: str, allocated: int) -> Group:
    """Group."""
    return Group((entry(name, allocated=allocated),))


def make_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Make pair."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    return src, dst


def _copy_tree(src: Path, dst: Path, names: list[str]) -> None:
    """Copy tree."""
    for name in names:
        target = dst / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if (src / name).is_symlink():
            target.symlink_to((src / name).readlink())
        else:
            shutil.copy2(src / name, target)


def scanned(src: Path) -> list[Group]:
    """Return the scanned groups of a tree."""
    return group_by_inode(scan(src), src)


def test_scan_lists_non_directories_and_skips_root_zfs(tmp_path: Path) -> None:
    """Scan lists non directories and skips root zfs."""
    (tmp_path / "file").write_text("x")
    (tmp_path / "link").symlink_to("file")
    os.mkfifo(tmp_path / "fifo")
    (tmp_path / "empty").mkdir()
    (tmp_path / ".zfs").mkdir()
    (tmp_path / ".zfs" / "x").write_text("x")
    (tmp_path / "sub" / ".zfs").mkdir(parents=True)
    (tmp_path / "sub" / ".zfs" / "x").write_text("x")
    names = sorted(e.name for e in scan(tmp_path))
    assert names == ["fifo", "file", "link", str(Path("sub", ".zfs", "x"))]


def test_scan_records_inode_and_allocation(tmp_path: Path) -> None:
    """Scan records inode and allocation."""
    (tmp_path / "f").write_bytes(b"x" * 10000)
    (found,) = scan(tmp_path)
    st = os.lstat(tmp_path / "f")
    assert (found.ino, found.nlink, found.ctime_ns) == (st.st_ino, 1, st.st_ctime_ns)
    # A fresh file's blocks may not be written out yet; it counts as at least one 4K sector.
    assert found.allocated == max(st.st_blocks * 512, 4096)


def test_scan_counts_each_non_empty_file_as_at_least_one_sector(tmp_path: Path) -> None:
    """An encrypted dataset cannot embed a tiny file in its block pointer, so its copy takes a whole 4K sector."""
    (tmp_path / "tiny").write_bytes(b"x")
    with (tmp_path / "sparse").open("wb") as handle:
        handle.truncate(100)
    (tmp_path / "empty").touch()
    (tmp_path / "link").symlink_to("tiny")
    assert os.lstat(tmp_path / "sparse").st_blocks * 512 < 4096  # noqa: PLR2004
    allocated = {item.name: item.allocated for item in scan(tmp_path)}
    assert allocated == {
        "tiny": 4096,
        "sparse": 4096,
        # Only regular files with data: an empty file has no block to write, a symlink keeps its own count.
        "empty": os.lstat(tmp_path / "empty").st_blocks * 512,
        "link": os.lstat(tmp_path / "link").st_blocks * 512,
    }


def test_scan_odd_names(tmp_path: Path) -> None:
    """Scan odd names."""
    odd = ["a b", "new\nline", "-dash", BAD_NAME]
    for name in odd:
        (tmp_path / name).write_text("x")
    assert sorted(e.name for e in scan(tmp_path)) == sorted(odd)


def test_scan_refuses_foreign_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan refuses foreign device."""
    (tmp_path / "mnt").mkdir()
    real = os.lstat

    def fake(path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result:
        st = real(path, *args, **kwargs)  # type: ignore[arg-type]
        if Path(path).name == "mnt":
            values = list(st)
            values[stat.ST_DEV] = st.st_dev + 1
            return os.stat_result(values)
        return st

    monkeypatch.setattr(os, "lstat", fake)
    with pytest.raises(MigrationError) as exc:
        scan(tmp_path)
    assert exc.value.message == f"{tmp_path / 'mnt'} is another filesystem mounted inside {tmp_path}"


def test_group_by_inode_joins_hardlinks(tmp_path: Path) -> None:
    """Group by inode joins hardlinks."""
    (tmp_path / "b").write_text("x")
    os.link(tmp_path / "b", tmp_path / "a")
    (tmp_path / "c").write_text("y")
    groups = scanned(tmp_path)
    assert [g.first for g in groups] == ["a", "c"]
    assert [e.name for e in groups[0].entries] == ["a", "b"]


def test_group_allocated_counts_inode_once(tmp_path: Path) -> None:
    """Group allocated counts inode once."""
    (tmp_path / "a").write_bytes(b"x" * 10000)
    os.link(tmp_path / "a", tmp_path / "b")
    (g,) = scanned(tmp_path)
    assert g.allocated == max(os.lstat(tmp_path / "a").st_blocks * 512, 4096)


def test_group_by_inode_refuses_missing_link(tmp_path: Path) -> None:
    """Group by inode refuses missing link."""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").write_text("x")
    os.link(tmp_path / "outside", root / "inside")
    with pytest.raises(MigrationError) as exc:
        group_by_inode(scan(root), root)
    assert exc.value.message == f"{root / 'inside'} has 2 hard links but only 1 were found under {root}"


def test_plan_batches_respects_size() -> None:
    """Plan batches respects size."""
    groups = [group("a", 4), group("b", 4), group("c", 4), group("d", 1)]
    batches = plan_batches(groups, 8)
    assert [[g.first for g in b] for b in batches] == [["a", "b"], ["c", "d"]]


def test_plan_batches_big_group_alone() -> None:
    """Plan batches big group alone."""
    groups = [group("a", 1), group("big", 50), group("b", 1)]
    batches = plan_batches(groups, 8)
    assert [[g.first for g in b] for b in batches] == [["a"], ["big"], ["b"]]
    assert plan_batches([], 8) == []


def test_write_file_list_nul_separated(tmp_path: Path) -> None:
    """Every name is NUL-terminated and starts with ./, so rsync never reads a # or ; name as a comment."""
    names = ["new\nline", BAD_NAME, "plain", "#hash", ";semi", "#dir/f"]
    groups = [Group((entry(n),)) for n in names]
    out = tmp_path / "list"
    write_file_list(out, groups)
    raw = out.read_bytes()
    assert raw.endswith(b"\0")
    assert [os.fsdecode(p) for p in raw.split(b"\0")[:-1]] == [f"./{name}" for name in names]


def test_verify_ok_after_copy(tmp_path: Path) -> None:
    """Verify ok after copy."""
    src, dst = make_pair(tmp_path)
    (src / "d").mkdir()
    (src / "d" / "f").write_text("hello")
    os.link(src / "d" / "f", src / "h")
    (src / "s").symlink_to("d/f")
    os.mkfifo(src / "fifo")
    groups = scanned(src)
    _copy_tree(src, dst, ["d/f", "s"])
    os.mkfifo(dst / "fifo")
    (dst / "fifo").chmod(stat.S_IMODE(os.lstat(src / "fifo").st_mode))
    os.link(dst / "d" / "f", dst / "h")
    verified, changed = verify(src, dst, groups, device=dst.stat().st_dev)
    assert verified == groups
    assert changed == []


def test_verify_changed_source_is_skipped(tmp_path: Path) -> None:
    """Verify changed source is skipped."""
    src, dst = make_pair(tmp_path)
    (src / "f").write_text("x")
    (src / "g").write_text("y")
    groups = scanned(src)
    _copy_tree(src, dst, ["f", "g"])
    os.utime(src / "f", ns=(1, 2))
    (src / "g").unlink()
    verified, changed = verify(src, dst, groups, device=dst.stat().st_dev)
    assert verified == []
    assert [g.first for g in changed] == ["f", "g"]


def verify_message(src: Path, dst: Path) -> str:
    """Verify message."""
    with pytest.raises(MigrationError) as exc:
        verify(src, dst, scanned(src), device=dst.stat().st_dev)
    return exc.value.message


def test_verify_missing_copy_raises(tmp_path: Path) -> None:
    """Verify missing copy raises."""
    src, dst = make_pair(tmp_path)
    (src / "f").write_text("x")
    assert verify_message(src, dst) == "f is missing from the copy"


def test_verify_copy_on_another_device_raises(tmp_path: Path) -> None:
    """A copy that is not on the staged dataset's device is never trusted, however well it matches."""
    src, dst = make_pair(tmp_path)
    (src / "f").write_text("x")
    _copy_tree(src, dst, ["f"])
    with pytest.raises(MigrationError) as exc:
        verify(src, dst, scanned(src), device=dst.stat().st_dev + 1)
    assert exc.value.message == "the copy of f is not on the staged dataset"


def test_mount_device_needs_a_mount_point(tmp_path: Path) -> None:
    """A plain or missing directory has no filesystem mounted on it; a mount point gives its device."""
    assert mount_device(tmp_path) in {None, tmp_path.stat().st_dev}
    (tmp_path / "plain").mkdir()
    assert mount_device(tmp_path / "plain") is None
    assert mount_device(tmp_path / "missing") is None
    proc = Path("/proc")
    if os.path.ismount(proc):
        assert mount_device(proc) == proc.stat().st_dev


def test_device_of_follows_symlinks_and_is_none_when_missing(tmp_path: Path) -> None:
    """A symlink gives its target's device; a missing file, or one under a file, has none."""
    (tmp_path / "key").write_text("k")
    (tmp_path / "link").symlink_to(tmp_path / "key")
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    assert device_of(tmp_path / "link") == (tmp_path / "key").stat().st_dev
    assert device_of(tmp_path / "missing") is None
    assert device_of(tmp_path / "dangling") is None
    assert device_of(tmp_path / "key" / "below") is None
    proc = Path("/proc")
    if os.path.ismount(proc):
        assert device_of(proc / "self" / "mountinfo") != (tmp_path / "key").stat().st_dev


def mountinfo(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    """Make the given lines what /proc/self/mountinfo reads."""
    monkeypatch.setattr(filetree, "_read_mountinfo", lambda: os.fsencode("".join(f"{line}\n" for line in lines)))


def test_mounted_at_gives_source_and_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mount source and filesystem type of the line whose mount point is the path, any optional fields."""
    root = Path(os.path.realpath(tmp_path))
    mountinfo(
        monkeypatch,
        "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw",
        f"36 22 0:44 / {root}/media rw,noatime shared:20 master:3 - zfs tank/media rw,xattr,noacl",
        f"37 22 0:45 / {root}/music rw - zfs tank/music rw",
    )
    assert mounted_at(root / "media") == ("tank/media", "zfs")
    assert mounted_at(root / "music") == ("tank/music", "zfs")
    assert mounted_at(Path("/")) == ("/dev/sda1", "ext4")


def test_mounted_at_is_none_without_a_mount_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A path that is no mount point, or a directory inside one, has no line of its own."""
    root = Path(os.path.realpath(tmp_path))
    mountinfo(monkeypatch, f"36 22 0:44 / {root}/media rw shared:20 - zfs tank/media rw")
    assert mounted_at(root / "other") is None
    assert mounted_at(root / "media" / "sub") is None


def test_mounted_at_decodes_escaped_spaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r"""The kernel writes a space in a mount point as \040, so a trailing space zfs get strips is seen."""
    root = Path(os.path.realpath(tmp_path))
    mountinfo(
        monkeypatch,
        rf"36 22 0:44 / {root}/my\040media rw shared:20 - zfs tank/media rw",
        rf"37 22 0:45 / {root}/music\040 rw shared:21 - zfs tank/music rw",
        rf"38 22 0:46 / {root}/tab\011back\134slash rw shared:22 - zfs tank/odd rw",
    )
    assert mounted_at(root / "my media") == ("tank/media", "zfs")
    assert mounted_at(root / "music") is None
    assert mounted_at(root / "music ") == ("tank/music", "zfs")
    assert mounted_at(root / "tab\tback\\slash") == ("tank/odd", "zfs")


def test_mounted_at_gives_the_mount_on_top(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Of two mounts on one path, the later one covers the earlier; mountinfo lists it after."""
    root = Path(os.path.realpath(tmp_path))
    mountinfo(
        monkeypatch,
        f"36 22 0:44 / {root}/media rw shared:20 - zfs tank/media rw",
        f"40 36 0:50 / {root}/media rw shared:30 - tmpfs tmpfs rw",
    )
    assert mounted_at(root / "media") == ("tmpfs", "tmpfs")


def test_mounted_at_follows_symlinks_like_the_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kernel resolves symlinks in a mount's target, so mountinfo lists the real path."""
    root = Path(os.path.realpath(tmp_path))
    (root / "real").mkdir()
    (root / "link").symlink_to("real")
    mountinfo(monkeypatch, f"36 22 0:44 / {root}/real/media rw shared:20 - zfs tank/media rw")
    assert mounted_at(root / "link" / "media") == ("tank/media", "zfs")


@pytest.mark.skipif(not Path("/proc/self/mountinfo").exists(), reason="no /proc/self/mountinfo")
def test_mounted_at_reads_this_host() -> None:
    """The root directory is a mount point on every Linux host."""
    assert mounted_at(Path("/")) is not None


def test_verify_type_mismatch_raises(tmp_path: Path) -> None:
    """Verify type mismatch raises."""
    src, dst = make_pair(tmp_path)
    (src / "f").write_text("x")
    (dst / "f").mkdir()
    assert verify_message(src, dst) == "f differs from its copy (type)"


def test_verify_mode_mismatch_raises(tmp_path: Path) -> None:
    """Verify mode mismatch raises."""
    src, dst = make_pair(tmp_path)
    (src / "f").write_text("x")
    _copy_tree(src, dst, ["f"])
    (dst / "f").chmod(0o600)
    (src / "f").chmod(0o644)
    assert verify_message(src, dst) == "f differs from its copy (mode)"


def test_verify_owner_mismatch_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify owner mismatch raises."""
    src, dst = make_pair(tmp_path)
    (src / "f").write_text("x")
    _copy_tree(src, dst, ["f"])
    real = os.lstat

    def fake(path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result:
        st = real(path, *args, **kwargs)  # type: ignore[arg-type]
        if Path(path) == dst / "f":
            values = list(st)
            values[stat.ST_UID] = st.st_uid + 1
            return os.stat_result(values)
        return st

    groups = scanned(src)
    monkeypatch.setattr(os, "lstat", fake)
    with pytest.raises(MigrationError) as exc:
        verify(src, dst, groups, device=dst.stat().st_dev)
    assert exc.value.message == "f differs from its copy (uid)"


def test_verify_group_mismatch_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify group mismatch raises."""
    src, dst = make_pair(tmp_path)
    (src / "f").write_text("x")
    _copy_tree(src, dst, ["f"])
    real = os.lstat

    def fake(path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result:
        st = real(path, *args, **kwargs)  # type: ignore[arg-type]
        if Path(path) == dst / "f":
            values = list(st)
            values[stat.ST_GID] = st.st_gid + 1
            return os.stat_result(values)
        return st

    groups = scanned(src)
    monkeypatch.setattr(os, "lstat", fake)
    with pytest.raises(MigrationError) as exc:
        verify(src, dst, groups, device=dst.stat().st_dev)
    assert exc.value.message == "f differs from its copy (gid)"


def test_verify_size_mismatch_raises(tmp_path: Path) -> None:
    """Verify size mismatch raises."""
    src, dst = make_pair(tmp_path)
    (src / "f").write_text("xx")
    _copy_tree(src, dst, ["f"])
    st = os.lstat(src / "f")
    (dst / "f").write_text("x")
    os.utime(dst / "f", ns=(st.st_atime_ns, st.st_mtime_ns))
    assert verify_message(src, dst) == "f differs from its copy (size)"


def test_verify_mtime_mismatch_raises(tmp_path: Path) -> None:
    """Verify mtime mismatch raises."""
    src, dst = make_pair(tmp_path)
    (src / "f").write_text("x")
    _copy_tree(src, dst, ["f"])
    os.utime(dst / "f", ns=(1, 1))
    assert verify_message(src, dst) == "f differs from its copy (mtime)"


def test_verify_symlink_target_mismatch_raises(tmp_path: Path) -> None:
    """Verify symlink target mismatch raises."""
    src, dst = make_pair(tmp_path)
    (src / "s").symlink_to("a")
    (dst / "s").symlink_to("b")
    assert verify_message(src, dst) == "s differs from its copy (target)"


def test_verify_device_mismatch_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A device node whose st_rdev differs from its copy raises."""
    src, dst = make_pair(tmp_path)
    (src / "dev").write_text("x")
    real = os.lstat

    def fake(path: str | os.PathLike[str], *args: object, **kwargs: object) -> SimpleNamespace:
        st = real(path, *args, **kwargs)  # type: ignore[arg-type]
        fields = {n: getattr(st, n) for n in dir(st) if n.startswith("st_")}
        fields["st_mode"] = stat.S_IFCHR | 0o600
        fields["st_rdev"] = 5 if Path(path).parent == dst else 4
        return SimpleNamespace(**fields)

    (dst / "dev").write_text("x")
    groups = scanned(src)
    monkeypatch.setattr(os, "lstat", fake)
    with pytest.raises(MigrationError) as exc:
        verify(src, dst, groups, device=dst.stat().st_dev)
    assert exc.value.message == "dev differs from its copy (rdev)"


def test_verify_broken_hardlink_raises(tmp_path: Path) -> None:
    """Verify broken hardlink raises."""
    src, dst = make_pair(tmp_path)
    (src / "b").write_text("x")
    os.link(src / "b", src / "a")
    _copy_tree(src, dst, ["a", "b"])
    assert verify_message(src, dst) == "hard links of a were not kept in the copy"


def test_delete_unlinks_and_ignores_missing(tmp_path: Path) -> None:
    """Delete unlinks and ignores missing."""
    (tmp_path / "a").write_text("x")
    (tmp_path / "b").write_text("y")
    os.link(tmp_path / "b", tmp_path / "c")
    groups = scanned(tmp_path)
    (tmp_path / "a").unlink()
    assert delete(tmp_path, groups) == len(groups[1].entries)
    assert list(tmp_path.iterdir()) == []


def test_remove_scratch_restores_root_mtime(tmp_path: Path) -> None:
    """Remove scratch restores root mtime."""
    scratch = tmp_path / SCRATCH
    (scratch / "tmp").mkdir(parents=True)
    (scratch / "dirs" / "docs").mkdir(parents=True)
    os.utime(scratch / "dirs", ns=(1_000_000_000, 2_000_000_000))
    remove_scratch(tmp_path)
    assert not scratch.exists()
    st = os.lstat(tmp_path)
    assert (st.st_atime_ns, st.st_mtime_ns) == (1_000_000_000, 2_000_000_000)


def test_remove_scratch_after_a_stop_past_the_rename(tmp_path: Path) -> None:
    """A rerun after a stop part-way through the removal still restores the root's times, from the file."""
    scratch = tmp_path / SCRATCH
    (scratch / "dirs.removing" / "tv").mkdir(parents=True)
    (scratch / "root-times").write_text("1000000000 2000000000\n")
    remove_scratch(tmp_path)
    assert not scratch.exists()
    st = os.lstat(tmp_path)
    assert (st.st_atime_ns, st.st_mtime_ns) == (1_000_000_000, 2_000_000_000)


def test_remove_scratch_tolerates_partial_state(tmp_path: Path) -> None:
    """Whatever is left of scratch is removed; the root keeps its times only from a pristine copy."""
    (tmp_path / SCRATCH / "tmp").mkdir(parents=True)
    (tmp_path / SCRATCH / "files").write_text("x")
    remove_scratch(tmp_path)
    assert not (tmp_path / SCRATCH).exists()
    remove_scratch(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_delete_skips_group_replaced_after_verify(tmp_path: Path) -> None:
    """A name replaced by a new inode after verify is kept, with its whole group."""
    src, dst = make_pair(tmp_path)
    (src / "cfg").write_text("old")
    (src / "other").write_text("o")
    groups = scanned(src)
    _copy_tree(src, dst, ["cfg", "other"])
    verified, changed = verify(src, dst, groups, device=dst.stat().st_dev)
    assert changed == []
    (src / "cfg.new").write_text("new")
    (src / "cfg.new").replace(src / "cfg")
    assert delete(src, verified) == 1
    assert (src / "cfg").read_text() == "new"
    assert not (src / "other").exists()
