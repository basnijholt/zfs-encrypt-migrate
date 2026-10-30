"""A small stateful stand-in for zfs, answering the commands the migration issues."""

from __future__ import annotations

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
        return self.props.get((name, prop), DEFAULTS.get(prop, "0"))

    def ops(self) -> list[tuple[str, ...]]:
        """Return the ordered log of commands that matter for safety: writes, streams, syncs, written@ reads."""
        return [
            call
            for call in self.log
            if call[0] == "zpool" or call[1] in WRITE_VERBS or (call[1] == "get" and call[-2].startswith("written@"))
        ]

    def answer(self, args: Sequence[str]) -> CommandResult:
        """Log and answer one command."""
        self.log.append(tuple(args))
        return self._answer(args)

    def _answer(self, args: Sequence[str]) -> CommandResult:  # noqa: C901, PLR0911, PLR0912
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
            return self._send(args)
        elif verb == "recv":
            return self._recv(args)
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
        target = args[-1]
        if sent is None:
            return fail("cannot receive: failed to read from stream\n")
        send_args, source, snap = sent
        encrypting = any(arg.startswith("encryption=") for arg in args)
        if encrypting:
            if target in self.datasets:
                return fail(f"cannot receive new filesystem stream: destination '{target}' exists\n")
        else:
            if target not in self.datasets:
                return fail(f"cannot receive incremental stream: destination '{target}' does not exist\n")
            if self._get("encryption", target) != "off" and self._get("keystatus", target) != "available":
                return fail("cannot receive incremental stream: key must be loaded\n")
            base = send_args[send_args.index("-I") + 1].removeprefix("@") if "-I" in send_args else None
            if base is not None and f"{target}@{base}" not in self.snapshots:
                return fail(
                    "cannot receive incremental stream: most recent snapshot does not match incremental source\n",
                )
        encrypted = encrypting or self._get("encryption", target) != "off"
        for name in self._under(source):
            staged = target + name.removeprefix(source)
            created = staged not in self.datasets
            self.datasets.add(staged)
            self.snapshot(f"{staged}@{snap}")
            self.prop(staged, "encryptionroot", target)
            if encrypted:
                self.prop(staged, "encryption", "aes-256-gcm")
                if created:
                    self.prop(staged, "keystatus", "available")
        return ok()


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
