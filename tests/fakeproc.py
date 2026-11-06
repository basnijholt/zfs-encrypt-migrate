"""A fake /proc tree: processes with their mount namespace, mounts, open files and mapped files."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

HOST = "mnt:[4026531841]"
CONTAINER = "mnt:[4026532500]"
SERVICE = "mnt:[4026532600]"
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


def maps_line(path: Path, device: int, inode: int) -> str:
    """Return one line of /proc/PID/maps for ``path`` mapped from ``device``."""
    return f"7f0000000000-7f0000001000 r--p 00000000 {os.major(device):02x}:{os.minor(device):02x} {inode}  {path}\n"


class FakeProc:
    """Lay out ``root`` like /proc, with ``self`` the checking process in the host mount namespace."""

    def __init__(self, root: Path, mountinfo: str = "") -> None:
        """Create the checking process, which sees ``mountinfo``."""
        self.root = root
        self.add(SELF_PID, comm="zfs-encrypt-mig", mountinfo=mountinfo)
        # No flock held by anyone.
        (root / "locks").write_text("")
        (root / "self").symlink_to(str(SELF_PID))

    def add(
        self,
        pid: int,
        *,
        comm: str = "worker",
        ns: str = HOST,
        mountinfo: str = "",
        fds: Sequence[Path | str] = (),
        cwd: Path | None = None,
        exe: Path | None = None,
        maps: str = "",
        cgroup: str | None = None,
    ) -> Path:
        """Add a process and return its directory; links not given are left out, as for a process that is gone."""
        base = self.root / str(pid)
        (base / "ns").mkdir(parents=True)
        # The kernel's link names the namespace; it points nowhere a path could reach.
        (base / "ns" / "mnt").symlink_to(ns)
        (base / "comm").write_text(f"{comm}\n")
        (base / "mountinfo").write_text(mountinfo)
        (base / "fd").mkdir()
        for number, target in enumerate(fds, 3):
            (base / "fd" / str(number)).symlink_to(target)
        if cwd is not None:
            (base / "cwd").symlink_to(cwd)
        if exe is not None:
            (base / "exe").symlink_to(exe)
        (base / "maps").write_text(maps)
        if cgroup is not None:
            (base / "cgroup").write_text(f"0::{cgroup}\n")
        return base
