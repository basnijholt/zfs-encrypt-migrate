"""A small stateful stand-in for zfs, answering the commands the migration issues."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .conftest import fail, ok

if TYPE_CHECKING:
    from collections.abc import Sequence

    from zfs_encrypt_migrate.process import CommandResult

WRITE_VERBS = {"snapshot", "rename", "set", "mount", "unmount", "load-key", "unload-key", "create", "inherit", "recv"}
DEFAULTS = {
    "encryption": "off",
    "canmount": "on",
    "mounted": "no",
    "mountpoint": "/mnt",
    "keystatus": "-",
    "encryptionroot": "-",
    "used": "100",
}


class ZfsSim:
    """Datasets, snapshots and properties; unknown properties read from DEFAULTS."""

    def __init__(self, datasets: Sequence[str] = (), avail: int = 10**12) -> None:
        """Start with the given datasets."""
        self.datasets = set(datasets)
        self.snapshots: dict[str, str] = {}
        self.props: dict[tuple[str, str], str] = {}
        self.avail = avail
        self.busy: set[str] = set()
        self.roots: list[str] = []
        self._sending = ""

    def prop(self, name: str, prop: str, value: str) -> None:
        """Set a property value."""
        self.props[name, prop] = value

    def snapshot(self, name: str, guid: str | None = None) -> None:
        """Add a snapshot; the default GUID is the same for counterparts under different roots."""
        dataset, snap = name.split("@")
        relative = dataset
        for root in self.roots:
            if dataset == root or dataset.startswith(f"{root}/"):
                relative = dataset.removeprefix(root)
        self.snapshots[name] = guid or f"{snap}:{relative}"

    def _under(self, name: str) -> list[str]:
        return sorted(d for d in self.datasets if d == name or d.startswith(f"{name}/"))

    def _get(self, prop: str, name: str) -> str:
        if "@" in name and prop == "guid":
            return self.snapshots[name]
        return self.props.get((name, prop), DEFAULTS.get(prop, "0"))

    def answer(self, args: Sequence[str]) -> CommandResult:  # noqa: C901, PLR0911, PLR0912
        """Answer one command."""
        verb = args[1]
        if args[0] == "zpool":
            return ok()
        if verb == "list" and "avail" in args:
            return ok(f"{self.avail}\n")
        if verb == "list" and args[2] != "-rH":
            name = args[-1]
            if name in self.datasets or name in self.snapshots:
                return ok(f"{name}\n")
            return fail(f"cannot open '{name}': dataset does not exist\n")
        if verb == "list":
            return ok("".join(f"{d}\n" for d in self._under(args[-1])))
        if verb == "get":
            return ok("".join(f"{self._get(p, args[-1])}\n" for p in args[-2].split(",")))
        if verb == "snapshot":
            name = args[-1]
            dataset = name.split("@")[0]
            for d in self._under(dataset) if "-r" in args else [dataset]:
                self.snapshot(f"{d}@{name.split('@')[1]}")
            return ok()
        if verb == "unmount":
            if args[-1] in self.busy:
                return fail(f"cannot unmount '{args[-1]}': pool or dataset is busy\n")
            self.prop(args[-1], "mounted", "no")
        elif verb == "mount":
            self.prop(args[-1], "mounted", "yes")
        elif verb == "load-key":
            self.prop(args[-1], "keystatus", "available")
        elif verb == "unload-key":
            for d in self._under(args[-1]):
                self.prop(d, "keystatus", "unavailable")
        elif verb == "set":
            prop, value = args[2].split("=", 1)
            self.prop(args[-1], prop, value)
        elif verb == "send":
            self._sending = args[-1]
        elif verb == "recv":
            self._recv(args)
        elif verb == "create":
            self.datasets.add(args[-1])
        elif verb == "rename":
            old, new = args[-2:]
            self.datasets = {
                new + d.removeprefix(old) if d == old or d.startswith(f"{old}/") else d for d in self.datasets
            }
            self.props = {
                (new + n.removeprefix(old) if n == old or n.startswith(f"{old}/") else n, p): v
                for (n, p), v in self.props.items()
            }
        return ok()

    def _recv(self, args: Sequence[str]) -> None:
        """Mirror the sent snapshot (and descendants) under the target, which becomes its encryption root."""
        target = args[-1]
        source, snap = self._sending.split("@")
        for name in self._under(source):
            staged = target + name.removeprefix(source)
            self.datasets.add(staged)
            self.snapshot(f"{staged}@{snap}")
            self.prop(staged, "encryptionroot", target)


def mutations(calls: Sequence[Sequence[str]]) -> list[tuple[str, ...]]:
    """Filter recorded commands down to those that change state."""
    return [tuple(c) for c in calls if c[0] == "zpool" or c[1] in WRITE_VERBS]
