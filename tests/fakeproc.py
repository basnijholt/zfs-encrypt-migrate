"""A fake /proc tree: processes and their threads, each with the mounts it sees."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

SELF_PID = 4000


def mount_line(
    mount_id: int,
    parent: int,
    point: Path | str,
    source: str,
    *,
    root: str = "/",
    tags: str = "",
    fstype: str = "zfs",
) -> str:
    """Return one line of mountinfo; ``tags`` are the optional fields, such as ``shared:20 master:3``."""
    optional = f" {tags}" if tags else ""
    return f"{mount_id} {parent} 0:{mount_id} {root} {point} rw{optional} - {fstype} {source} rw\n"


class FakeProc:
    """Lay out ``root`` like /proc, with ``self`` the checking process."""

    def __init__(self, root: Path, mountinfo: str = "") -> None:
        """Create the checking process, which sees ``mountinfo``."""
        self.root = root
        self.add(SELF_PID, mountinfo=mountinfo)
        (root / "self").symlink_to(str(SELF_PID))

    def add(self, pid: int, *, mountinfo: str = "", threads: Mapping[int, str] = {}) -> Path:
        """Add a process and return its directory; ``threads`` maps more thread IDs to the mounts each sees."""
        base = self.root / str(pid)
        base.mkdir(parents=True)
        (base / "mountinfo").write_bytes(mountinfo.encode("utf-8", "surrogateescape"))
        for tid, mounts in {pid: mountinfo, **threads}.items():
            (base / "task" / str(tid)).mkdir(parents=True)
            (base / "task" / str(tid) / "mountinfo").write_bytes(mounts.encode("utf-8", "surrogateescape"))
        return base
