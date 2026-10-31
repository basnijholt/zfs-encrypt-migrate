"""A small stateful stand-in for zfs, answering the commands the migration issues."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .conftest import fail, ok

if TYPE_CHECKING:
    from collections.abc import Sequence

    from zfs_encrypt_migrate.process import CommandResult

WRITE_VERBS = {
    "snapshot",
    "rename",
    "set",
    "mount",
    "unmount",
    "load-key",
    "unload-key",
    "create",
    "inherit",
    "send",
    "recv",
}
DEFAULTS = {
    "encryption": "off",
    "canmount": "on",
    "mounted": "no",
    "mountpoint": "/mnt",
    "keystatus": "-",
    "encryptionroot": "-",
    "origin": "-",
    "dedup": "off",
    "used": "100",
}

# zpool get -p prints a ratio without its x.
POOL_DEFAULTS = {"dedupratio": "1.00", "dedup_table_size": "0"}


class ZfsSim:
    """Datasets, snapshots and properties; unknown properties read from DEFAULTS."""

    def __init__(self, datasets: Sequence[str] = (), avail: int = 10**12) -> None:
        """Start with the given datasets."""
        self.datasets = set(datasets)
        self.snapshots: dict[str, str] = {}
        self.props: dict[tuple[str, str], str] = {}
        # (dataset, property) -> (value, origin dataset), read with source "inherited from <origin>".
        self.inherited: dict[tuple[str, str], tuple[str, str]] = {}
        self.avail = avail
        # (pool, property) -> value, read by zpool get; unset ones read POOL_DEFAULTS, else "-".
        self.pool_props: dict[tuple[str, str], str] = {}
        self.busy: set[str] = set()
        # Datasets mounted with "zfs mount -o noatime"; until unmounted, zfs reports atime=off as temporary.
        self.noatime: set[str] = set()
        # Datasets in the order zfs mount mounted them; a later mount on the same path covers an earlier one.
        self.mount_order: list[str] = []
        self.volumes: set[str] = set()
        self.roots: list[str] = []
        self.log: list[tuple[str, ...]] = []
        self._sent: tuple[Sequence[str], str, str] | None = None

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
        if prop == "type":
            return "volume" if name in self.volumes else "filesystem"
        return self.props.get((name, prop), DEFAULTS.get(prop, "0"))

    def _get_all(self, name: str) -> str:
        """Answer ``zfs get -H -p -o property,value,source all``: local, inherited, then default properties."""
        # Read-only properties have no source, as in real zfs.
        readonly = {"mounted", "keystatus", "encryptionroot"}
        rows = {
            prop: (value, "-" if prop in readonly else "local") for (n, prop), value in self.props.items() if n == name
        }
        for (n, prop), (value, origin) in self.inherited.items():
            if n == name and prop not in rows:
                rows[prop] = (value, f"inherited from {origin}")
        for prop, value in DEFAULTS.items():
            rows.setdefault(prop, (value, "default"))
        # Real zfs hides the dataset's own atime behind a differing temporary mount option.
        if name in self.noatime and rows.get("atime", ("on", "default"))[0] != "off":
            rows["atime"] = ("off", "temporary")
        return "".join(f"{prop}\t{value}\t{source}\n" for prop, (value, source) in rows.items())

    def mountinfo(self) -> bytes:
        """Answer a read of /proc/self/mountinfo: a zfs line for each mounted dataset with a path, in mount order.

        Datasets a test marked mounted come first, as if mounted before the simulation began. Like the kernel,
        it lists the real path, with a space, tab, newline or backslash written as an octal escape.
        """
        mounted = [name for (name, prop), value in self.props.items() if prop == "mounted" and value == "yes"]
        mounted.sort(key=lambda name: self.mount_order.index(name) if name in self.mount_order else -1)
        lines = []
        for number, name in enumerate(mounted, 100):
            point = self.props.get((name, "mountpoint"), "")
            if point.startswith("/"):
                path = os.fsencode(os.path.realpath(point))
                escaped = re.sub(rb"[ \t\n\\]", lambda m: b"\\%03o" % m[0][0], path)
                lines.append(b"%d 1 0:%d / %s rw - zfs %s rw\n" % (number, number, escaped, name.encode()))
        return b"".join(lines)

    def ops(self) -> list[tuple[str, ...]]:
        """Return the ordered log of commands that matter for safety: writes, streams, syncs, written@ reads."""
        return [
            call
            for call in self.log
            if call[:2] == ("zpool", "sync")
            or (call[0] == "zfs" and call[1] in WRITE_VERBS)
            or (call[1] == "get" and call[-2].startswith("written@"))
        ]

    def answer(self, args: Sequence[str]) -> CommandResult:
        """Log and answer one command."""
        self.log.append(tuple(args))
        return self._answer(args)

    def _answer(self, args: Sequence[str]) -> CommandResult:  # noqa: C901, PLR0911, PLR0912
        verb = args[1]
        if args[0] == "zpool":
            if verb == "get":
                default = POOL_DEFAULTS.get(args[-2], "-")
                return ok(f"{self.pool_props.get((args[-1], args[-2]), default)}\n")
            return ok()
        if verb == "list" and "avail" in args:
            return ok(f"{self.avail}\n")
        if verb == "list" and "snapshot" in args:
            prefix = f"{args[-1]}@"
            return ok("".join(f"{s}\n" for s in sorted(self.snapshots) if s.startswith(prefix)))
        if verb == "list" and args[2] != "-rH":
            name = args[-1]
            if name in self.datasets or name in self.snapshots:
                return ok(f"{name}\n")
            return fail(f"cannot open '{name}': dataset does not exist\n")
        if verb == "list":
            names = self._under(args[-1])
            if "-d" in args:
                # Only the dataset itself and its direct children, as zfs list -d 1 does.
                names = [d for d in names if d == args[-1] or d.rsplit("/", 1)[0] == args[-1]]
            if "name,type" in args:
                return ok("".join(f"{d}\t{'volume' if d in self.volumes else 'filesystem'}\n" for d in names))
            return ok("".join(f"{d}\n" for d in names))
        if verb == "get" and args[-2] == "all":
            return ok(self._get_all(args[-1]))
        if verb == "get" and "-r" in args:
            return self._get_local(args[-2], args[-1])
        if verb == "get":
            if (
                args[-2].startswith("written@")
                and f"{args[-1]}@{args[-2].removeprefix('written@')}" not in self.snapshots
            ):
                return fail(f"cannot open '{args[-1]}@{args[-2].removeprefix('written@')}': snapshot does not exist\n")
            return ok("".join(f"{self._get(p, args[-1])}\n" for p in args[-2].split(",")))
        if verb == "snapshot":
            name = args[-1]
            dataset = name.split("@")[0]
            for d in self._under(dataset) if "-r" in args else [dataset]:
                self.snapshot(f"{d}@{name.split('@')[1]}")
            return ok()
        if verb == "unmount":
            return self._unmount(args[-1])
        if verb == "mount":
            self._mount(args)
        elif verb == "load-key":
            if "-n" in args:
                return ok()
            self.prop(args[-1], "keystatus", "available")
        elif verb == "unload-key":
            for d in self._under(args[-1]):
                self.prop(d, "keystatus", "unavailable")
        elif verb == "set":
            prop, value = args[2].split("=", 1)
            self.prop(args[-1], prop, value)
        elif verb == "send":
            return self._send(args)
        elif verb == "recv":
            return self._recv(args)
        elif verb == "inherit":
            self.props.pop((args[-1], args[-2]), None)
        elif verb == "create":
            self._create(args)
        elif verb == "rename":
            old, new = args[-2:]
            self.datasets = {self._renamed(d, old, new) for d in self.datasets}
            self.volumes = {self._renamed(d, old, new) for d in self.volumes}
            self.props = {(self._renamed(n, old, new), p): v for (n, p), v in self.props.items()}
            # Snapshots travel with their dataset.
            self.snapshots = {
                self._renamed(name.split("@")[0], old, new) + "@" + name.split("@")[1]: guid
                for name, guid in self.snapshots.items()
            }
        return ok()

    def _get_local(self, prop: str, root: str) -> CommandResult:
        """Answer ``zfs get -H -r -s local -t filesystem -o name,value PROP ROOT``."""
        names = [d for d in self._under(root) if d not in self.volumes and (d, prop) in self.props]
        return ok("".join(f"{d}\t{self.props[d, prop]}\n" for d in names))

    def _unmount(self, name: str) -> CommandResult:
        if name in self.busy:
            return fail(f"cannot unmount '{name}': pool or dataset is busy\n")
        self.prop(name, "mounted", "no")
        self.noatime.discard(name)
        if name in self.mount_order:
            self.mount_order.remove(name)
        return ok()

    def _mount(self, args: Sequence[str]) -> None:
        self.prop(args[-1], "mounted", "yes")
        if args[-1] in self.mount_order:
            self.mount_order.remove(args[-1])
        self.mount_order.append(args[-1])
        if "noatime" in args:
            self.noatime.add(args[-1])
        # Only an explicitly set mountpoint: the DEFAULTS one is a placeholder, never a real path.
        mountpoint = self.props.get((args[-1], "mountpoint"), "")
        if mountpoint.startswith("/"):
            Path(mountpoint).mkdir(parents=True, exist_ok=True)

    def _create(self, args: Sequence[str]) -> None:
        self.datasets.add(args[-1])
        for index, arg in enumerate(args):
            if arg == "-o":
                prop, value = args[index + 1].split("=", 1)
                self.prop(args[-1], prop, value)

    def _locked(self, name: str) -> bool:
        return self._get("encryption", name) != "off" and self._get("keystatus", name) != "available"

    @staticmethod
    def _renamed(name: str, old: str, new: str) -> str:
        return new + name.removeprefix(old) if name == old or name.startswith(f"{old}/") else name

    def _send(self, args: Sequence[str]) -> CommandResult:
        """Accept a stream only when its snapshots exist."""
        source, snap = args[-1].split("@")
        missing = [args[-1]]
        if "-I" in args:
            missing.append(f"{source}{args[args.index('-I') + 1]}")
        for name in missing:
            if name not in self.snapshots:
                self._sent = None
                return fail(f"cannot open '{name}': dataset does not exist\n")
        self._sent = (args, source, snap)
        return ok()

    def _recv(self, args: Sequence[str]) -> CommandResult:
        """Mirror the sent snapshot (and descendants) under the target, which becomes its encryption root."""
        sent, self._sent = self._sent, None
        if sent is None:
            return fail("cannot receive: failed to read from stream\n")
        send_args, source, snap = sent
        target = args[-1]
        refusal = self._recv_refusal(args, send_args)
        if refusal is not None:
            return refusal
        encrypting = any(arg.startswith("encryption=") for arg in args)
        # A new dataset under an encrypted parent inherits its encryption only when told to with -x.
        inherit = "-x" in args and args[args.index("-x") + 1] == "encryption"
        for name in self._under(source) if "-R" in send_args else [source]:
            staged = target + name.removeprefix(source)
            if staged not in self.datasets:
                self._create_received(staged, target, encrypting=encrypting, inherit=inherit)
            self.snapshot(f"{staged}@{snap}")
        return ok()

    def _recv_refusal(self, args: Sequence[str], send_args: Sequence[str]) -> CommandResult | None:
        """Return the failure real zfs gives for a stream that does not fit the target, if any."""
        if "-I" in send_args:
            return self._incremental_refusal(args[-1], send_args[send_args.index("-I") + 1].removeprefix("@"))
        target = args[-1]
        if target in self.datasets:
            return fail(f"cannot receive new filesystem stream: destination '{target}' exists\n")
        parent = target.rsplit("/", 1)[0]
        encrypting = any(arg.startswith("encryption=") for arg in args)
        if not encrypting and (parent not in self.datasets or self._locked(parent)):
            return fail(f"cannot receive new filesystem stream: parent '{parent}' is missing or locked\n")
        return None

    def _incremental_refusal(self, target: str, base: str) -> CommandResult | None:
        if target not in self.datasets:
            return fail(f"cannot receive incremental stream: destination '{target}' does not exist\n")
        if self._locked(target):
            return fail("cannot receive incremental stream: key must be loaded\n")
        if f"{target}@{base}" not in self.snapshots:
            return fail("cannot receive incremental stream: most recent snapshot does not match incremental source\n")
        return None

    def _create_received(self, staged: str, target: str, *, encrypting: bool, inherit: bool) -> None:
        self.datasets.add(staged)
        parent = staged.rsplit("/", 1)[0]
        if encrypting:
            # Everything created by an encrypting receive joins the new encryption root.
            self.prop(staged, "encryptionroot", target)
            self.prop(staged, "encryption", "aes-256-gcm")
            self.prop(staged, "keystatus", "available")
        elif inherit and self._get("encryption", parent) != "off":
            self.prop(staged, "encryptionroot", self._get("encryptionroot", parent))
            self.prop(staged, "encryption", "aes-256-gcm")
            self.prop(staged, "keystatus", self._get("keystatus", parent))
        else:
            self.prop(staged, "encryptionroot", "-")


def staged_ready(sim: ZfsSim, stage: str = "tank/stage", *, final: bool = False) -> None:
    """Set up the state after a seed of tank/data: encrypted staged tree, key unloaded."""
    sim.datasets |= {stage, f"{stage}/old", f"{stage}/data", f"{stage}/data/child"}
    pairs = [("tank/data", f"{stage}/data"), ("tank/data/child", f"{stage}/data/child")]
    for source, staged in pairs:
        sim.snapshot(f"{source}@enc-seed")
        sim.snapshot(f"{staged}@enc-seed")
        sim.prop(staged, "encryptionroot", f"{stage}/data")
        sim.prop(staged, "encryption", "aes-256-gcm")
        sim.prop(staged, "keystatus", "unavailable")
        if final:
            sim.snapshot(f"{source}@enc-final")
            sim.snapshot(f"{staged}@enc-final")
