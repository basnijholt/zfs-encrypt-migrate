"""Thin wrapper over the zfs and zpool commands."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

import typer

from .constants import COMMAND_STARTUP_ERROR_RETURNCODE, DATASET_NAME_RE
from .errors import ZfsError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .process import CommandResult, CommandRunner

# A freshly received volume is held open by udev for a moment, which makes unload-key report "busy".
_UNLOAD_ATTEMPTS = 20
_UNLOAD_RETRY_SECONDS = 0.5

# -13 from subprocess, 128 + 13 when the kill is reported through a shell.
_SIGPIPE_RETURNCODES = frozenset({-13, 141})


def validate_name(name: str) -> str:
    """Return a dataset or pool name, refusing anything that could act as an option or a snapshot."""
    # fullmatch, not match with "$": "$" would let a trailing newline through.
    # The charset allows "-", so a leading one must be refused separately to block option injection.
    if name.startswith("-") or DATASET_NAME_RE.fullmatch(name) is None:
        msg = f"invalid dataset name {name!r}: use letters, digits and _ . : - separated by /"
        raise typer.BadParameter(msg)
    return name


def validate_child(name: str) -> str:
    """Return the name of a direct child dataset, refusing paths and anything validate_name refuses."""
    validate_name(name)
    if "/" in name:
        msg = f"invalid child name {name!r}: give the name of a direct child, without /"
        raise typer.BadParameter(msg)
    return name


class Zfs:
    """Run zfs and zpool commands, raising ZfsError on any unexpected failure."""

    def __init__(self, runner: CommandRunner) -> None:
        """Use the given runner for every command."""
        self.runner = runner

    def _run(self, args: Sequence[str]) -> CommandResult:
        result = self.runner.run(args)
        if result.returncode != 0:
            raise ZfsError(result, args)
        return result

    def exists(self, name: str) -> bool:
        """Return whether a dataset exists; any failure other than "does not exist" raises."""
        args = ["zfs", "list", "-H", "-o", "name", name]
        result = self.runner.run(args)
        if result.returncode == 0:
            return True
        if result.returncode != COMMAND_STARTUP_ERROR_RETURNCODE and "does not exist" in result.stderr:
            return False
        raise ZfsError(result, args)

    def list_encryption(self, pools: Sequence[str]) -> list[tuple[str, str]]:
        """List every filesystem and volume of the pools with its encryption property."""
        result = self._run(["zfs", "list", "-rH", "-t", "filesystem,volume", "-o", "name,encryption", *pools])
        rows = (line.split("\t") for line in result.stdout.splitlines() if line)
        return [(row[0], row[1]) for row in rows]

    def get(self, prop: str, name: str) -> str:
        """Read one property as a parsable value."""
        return self._run(["zfs", "get", "-H", "-p", "-o", "value", prop, name]).stdout.strip()

    def subtree(self, name: str) -> list[str]:
        """List a dataset and its descendants, parents first."""
        result = self._run(["zfs", "list", "-rH", "-t", "filesystem,volume", "-o", "name", "-s", "name", name])
        return result.stdout.split()

    def filesystems(self, name: str) -> list[str]:
        """List a dataset and its descendant filesystems, without volumes, parents first."""
        # Filtered here: zfs list -t filesystem refuses a volume named as the root.
        result = self._run(["zfs", "list", "-rH", "-t", "filesystem,volume", "-o", "name,type", "-s", "name", name])
        rows = (line.split("\t") for line in result.stdout.splitlines() if line)
        return [row[0] for row in rows if row[1] == "filesystem"]

    def children(self, name: str) -> list[str]:
        """List the filesystems and volumes directly under a dataset."""
        result = self._run(["zfs", "list", "-rH", "-t", "filesystem,volume", "-o", "name", "-d", "1", name])
        return [child for child in result.stdout.split() if child != name]

    def snapshot(self, name: str, *, recursive: bool) -> None:
        """Create a snapshot, recursively when asked."""
        self._run(["zfs", "snapshot", *(["-r"] if recursive else []), name])

    def send_recv(self, send_args: Sequence[str], recv_args: Sequence[str]) -> None:
        """Run ``zfs send | zfs recv``, raising for whichever side failed."""
        send = ["zfs", "send", *send_args]
        recv = ["zfs", "recv", *recv_args]
        sent, received = self.runner.pipeline(send, recv)
        if received.returncode != 0:
            # A failing recv kills send with SIGPIPE; that is a symptom, not a second failure.
            if sent.returncode == 0 or (sent.returncode in _SIGPIPE_RETURNCODES and not sent.stderr.strip()):
                raise ZfsError(received, recv)
            send_detail = sent.stderr.strip() or f"exit {sent.returncode}"
            both = replace(received, stderr=f"{received.stderr.strip()}; zfs send also failed: {send_detail}")
            raise ZfsError(both, recv)
        if sent.returncode != 0:
            raise ZfsError(sent, send)

    def rename(self, old: str, new: str) -> None:
        """Rename without mounting anything."""
        self._run(["zfs", "rename", "-u", old, new])

    def set_prop(self, name: str, prop: str, value: str) -> None:
        """Set a property."""
        self._run(["zfs", "set", f"{prop}={value}", name])

    def inherit(self, name: str, prop: str) -> None:
        """Reset a property to its inherited value."""
        self._run(["zfs", "inherit", prop, name])

    def create(self, name: str, props: Mapping[str, str]) -> None:
        """Create a filesystem with the given properties."""
        options = [arg for key, value in props.items() for arg in ("-o", f"{key}={value}")]
        self._run(["zfs", "create", *options, name])

    def mount(self, name: str) -> None:
        """Mount a dataset."""
        self._run(["zfs", "mount", name])

    def unmount(self, name: str) -> bool:
        """Unmount a dataset; return False when it is busy. Never forces."""
        args = ["zfs", "unmount", name]
        result = self.runner.run(args)
        if result.returncode == 0:
            return True
        if "busy" in result.stderr:
            return False
        raise ZfsError(result, args)

    def load_key(self, name: str) -> None:
        """Load the encryption key using the dataset's keylocation."""
        self._run(["zfs", "load-key", name])

    def unload_key(self, name: str) -> None:
        """Unload the key of a dataset and its descendants."""
        args = ["zfs", "unload-key", "-r", name]
        for _ in range(_UNLOAD_ATTEMPTS - 1):
            result = self.runner.run(args)
            if result.returncode == 0:
                return
            if result.returncode == COMMAND_STARTUP_ERROR_RETURNCODE or "busy" not in result.stderr:
                raise ZfsError(result, args)
            time.sleep(_UNLOAD_RETRY_SECONDS)
        self._run(args)

    def pool_avail(self, pool: str) -> int:
        """Return free bytes in a pool."""
        return int(self._run(["zfs", "list", "-Hp", "-o", "avail", pool]).stdout.strip())

    def pool_sync(self, pool: str) -> None:
        """Flush pending writes of a pool."""
        self._run(["zpool", "sync", pool])
