"""Seed, catch up and cut over one dataset to native encryption."""

from __future__ import annotations

import contextlib
import signal
import stat
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .errors import Interrupted, MigrationError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path
    from types import TracebackType

    from .zfs import Zfs

ENCRYPTION = "aes-256-gcm"
SEED_SNAPSHOT = "enc-seed"
FINAL_SNAPSHOT = "enc-final"
# Signals that end a run; the CLI turns each into a KeyboardInterrupt so the abort guard always runs.
INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@contextlib.contextmanager
def signals_ignored() -> Iterator[None]:
    """Ignore the interrupt signals so a second one cannot skip the cleanup inside.

    Children inherit the ignore on purpose: the same keypress or hangup that reaches us reaches them.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    saved = {number: signal.signal(number, signal.SIG_IGN) for number in INTERRUPT_SIGNALS}
    try:
        yield
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)


@dataclass(frozen=True)
class Settings:
    """Options shared by every migration command."""

    stage: str
    key_file: Path
    min_free: int
    keyformat: str
    final_keylocation: str


def leaf_of(dataset: str) -> str:
    """Return the last path component of a dataset name."""
    return dataset.rsplit("/", 1)[-1]


def pool_of(dataset: str) -> str:
    """Return the pool a dataset lives in."""
    return dataset.split("/", 1)[0]


class AbortGuard:
    """Make any failure leave the staged key unloaded and print how to recover.

    The staged tree carries the source's mountpoints, so its key must never stay loaded after a
    failure. ``phase`` and ``canmount_on`` are updated by ``swap_into_place`` so the hints match how
    far the swap got; ``cleanup`` lists what a rerun needs removed first. Hints are only attached
    once ``source`` is known.
    """

    def __init__(
        self,
        zfs: Zfs,
        settings: Settings,
        staged: str,
        *,
        source: str | None,
        leaf: str | None,
        label: str,
    ) -> None:
        """Remember what to clean up and which recovery hints apply."""
        self.zfs = zfs
        self.settings = settings
        self.staged = staged
        self.source = source
        self.leaf = leaf
        self.label = label
        self.phase = ""
        self.canmount_on: list[str] = []
        self.cleanup: list[str] = []
        self.mounted: list[str] = []
        self.notes: list[str] = []

    def __enter__(self) -> Self:
        """Start guarding."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Clean up and re-raise failures as a MigrationError carrying the hints."""
        if exc is None:
            return
        with signals_ignored():
            hints = self._unload_hint()
        hints += self._phase_hints()
        hints += self.notes
        if isinstance(exc, MigrationError):
            error = exc
        elif isinstance(exc, KeyboardInterrupt):
            error = Interrupted("interrupted")
        elif isinstance(exc, Exception):
            error = MigrationError(f"{type(exc).__name__}: {exc}")
        else:
            return
        error.hints = (*error.hints, *hints)
        if error is exc:
            return
        raise error from exc

    def _unload_hint(self) -> list[str]:
        # A busy mount is never forced; the key stays loaded so the data stays readable.
        for name in reversed(self.mounted):
            try:
                busy = not self.zfs.unmount(name)
            except MigrationError:
                busy = True
            if busy:
                return [f"could not unmount {name}; run: zfs unmount {name} && zfs unload-key -r {self.staged}"]
        try:
            if self.zfs.exists(self.staged) and self.zfs.get("keystatus", self.staged) == "available":
                self.zfs.unload_key(self.staged)
        except MigrationError:
            return [f"could not unload key; run: zfs unload-key -r {self.staged}"]
        return []

    def _phase_hints(self) -> list[str]:
        source = self.source
        if source is None:
            return []
        old = f"{self.settings.stage}/old/{self.leaf}"
        canmount = [f"  zfs set canmount=on {ds}" for ds in self.canmount_on]
        lines = [f"{self.label} aborted."]
        match self.phase:
            case "":
                lines.append(f"{source} may be unmounted; remount with: zfs mount -a")
            case "canmount":
                lines.append(f"{source} is unmounted and canmount may be noauto; restore with:")
                lines += [*canmount, "  zfs mount -a"]
            case "renamed-old":
                lines += [
                    f"the original is at {old} (canmount=noauto, readonly=on); put it back with:",
                    f"  zfs rename -u {old} {source}",
                    f"  zfs inherit readonly {source}",
                    *canmount,
                    "  zfs mount -a",
                ]
            case "renamed-new":
                lines += [
                    "the swap completed; only the final steps failed. Finish with:",
                    f"  zfs set keylocation={self.settings.final_keylocation} {source}",
                    "  zfs mount -a",
                ]
        return lines + self.cleanup


def abort_guard(
    zfs: Zfs,
    settings: Settings,
    staged: str,
    *,
    source: str | None = None,
    leaf: str | None = None,
    label: str = "cutover",
) -> AbortGuard:
    """Guard an operation on ``staged``; see AbortGuard."""
    return AbortGuard(zfs, settings, staged, source=source, leaf=leaf, label=label)


def encrypted_recv_args(settings: Settings, staged: str) -> list[str]:
    """Arguments of a ``zfs recv`` that creates ``staged`` as a new encryption root."""
    return [
        "-u",
        "-o",
        f"encryption={ENCRYPTION}",
        "-o",
        f"keyformat={settings.keyformat}",
        "-o",
        f"keylocation=file://{settings.key_file}",
        staged,
    ]


def ensure_container(zfs: Zfs, name: str) -> None:
    """Create a container dataset that never mounts, unless it exists."""
    if not zfs.exists(name):
        zfs.create(name, {"canmount": "off"})


def refuse_cross_pool(settings: Settings, dataset: str, command: str) -> None:
    """Refuse a dataset outside the stage pool: the swap renames across the two, which zfs cannot do."""
    if pool_of(dataset) != pool_of(settings.stage):
        msg = (
            f"{dataset} is on pool {pool_of(dataset)} but the stage is on {pool_of(settings.stage)}; "
            f"{command} cannot rename across pools"
        )
        raise MigrationError(msg)


def refuse_nested(settings: Settings, dataset: str) -> None:
    """Refuse a dataset that is the stage, inside it or around it: the copy or the swap would land in itself."""
    stage = settings.stage
    if dataset == stage or dataset.startswith(f"{stage}/") or stage.startswith(f"{dataset}/"):
        msg = f"{dataset} overlaps the stage {stage}; neither may be or contain the other"
        raise MigrationError(msg)


def in_flight(zfs: Zfs, settings: Settings) -> list[str]:
    """Datasets under the stage other than the stage, its ``old`` container and the old copies."""
    stage = settings.stage
    if not zfs.exists(stage):
        return []
    old = f"{stage}/old"
    return [name for name in zfs.subtree(stage) if name not in {stage, old} and not name.startswith(f"{old}/")]


def check_free_space(zfs: Zfs, settings: Settings, dataset: str, size_props: str) -> None:
    """Free space on the staging pool after the copy of ``dataset`` must stay above the floor."""
    pool = pool_of(settings.stage)
    avail = zfs.pool_avail(pool)
    used = sum(int(value) for value in zfs.get(size_props, dataset).split())
    if avail - used < settings.min_free:
        msg = f"free space on {pool} ({avail}) minus size of {dataset} ({used}) is below floor {settings.min_free}"
        raise MigrationError(msg)


def preflight_seed(zfs: Zfs, settings: Settings, dataset: str, size_props: str = "used") -> None:
    """Refuse to seed unless the key, the dataset, the stage and the free space are in order."""
    refuse_nested(settings, dataset)
    check_key_file(settings)
    leaf = leaf_of(dataset)
    if not zfs.exists(dataset):
        msg = f"dataset {dataset} does not exist"
        raise MigrationError(msg)
    if zfs.get("encryption", dataset) != "off":
        msg = f"{dataset} is already encrypted"
        raise MigrationError(msg)
    if in_flight(zfs, settings):
        msg = f"a migration is already in flight under {settings.stage}"
        raise MigrationError(msg)
    for name in (f"{settings.stage}/{leaf}", f"{settings.stage}/old/{leaf}"):
        if zfs.exists(name):
            msg = f"{name} already exists"
            raise MigrationError(msg)
    check_free_space(zfs, settings, dataset, size_props)


def check_key_file(settings: Settings) -> None:
    """Refuse unless the key file is a private regular file."""
    key_file = settings.key_file
    # Anything other than a private regular file could leak the passphrase. Symlinks (secret
    # managers use them) are followed: the target must be the regular file with mode 600.
    if not key_file.is_file():
        msg = f"key file {key_file} not found"
        raise MigrationError(msg)
    if stat.S_IMODE(key_file.stat().st_mode) != 0o600:  # noqa: PLR2004
        msg = f"key file {key_file} must have mode 600"
        raise MigrationError(msg)


def load_stage_key(zfs: Zfs, staged: str) -> None:
    """Load the staged key unless it is already loaded."""
    if zfs.get("keystatus", staged) != "available":
        zfs.load_key(staged)


def snaps_match(zfs: Zfs, source: str, staged_root: str, snap: str) -> bool:
    """Every dataset under ``source`` has ``snap`` with the same GUID as its counterpart under ``staged_root``."""
    for name in zfs.subtree(source):
        theirs = f"{staged_root}{name.removeprefix(source)}@{snap}"
        ours = f"{name}@{snap}"
        if not (zfs.exists(ours) and zfs.exists(theirs)):
            return False
        if zfs.get("guid", ours) != zfs.get("guid", theirs):
            return False
    return True


def dirty_since(zfs: Zfs, dataset: str, snap: str) -> list[str]:
    """Datasets under ``dataset`` with data written since ``snap``."""
    return [name for name in zfs.subtree(dataset) if zfs.get(f"written@{snap}", name) != "0"]


def outside_encryption_root(zfs: Zfs, root: str, encryption_root: str) -> str | None:
    """Return the first filesystem or volume under ``root`` whose encryption root is not ``encryption_root``."""
    return next((name for name in zfs.subtree(root) if zfs.get("encryptionroot", name) != encryption_root), None)


def check_encrypted_under(zfs: Zfs, root: str, encryption_root: str) -> None:
    """Every filesystem and volume under ``root`` has encryption root ``encryption_root``."""
    name = outside_encryption_root(zfs, root, encryption_root)
    if name is not None:
        msg = f"{name} is not encrypted under {encryption_root}"
        raise MigrationError(msg)


def unmount_subtree(zfs: Zfs, dataset: str) -> None:
    """Unmount ``dataset`` and its descendants, children first, never forcing."""
    for name in sorted(zfs.subtree(dataset), reverse=True):
        if zfs.get("mounted", name) == "yes" and not zfs.unmount(name):
            msg = f"cannot unmount {name}; a writer is probably still running"
            raise MigrationError(msg)


def seed(zfs: Zfs, settings: Settings, dataset: str) -> list[str]:
    """Copy ``dataset`` to an encrypted staged tree, leaving its key unloaded."""
    leaf = leaf_of(dataset)
    staged = f"{settings.stage}/{leaf}"
    preflight_seed(zfs, settings, dataset)
    with abort_guard(zfs, settings, staged):
        ensure_container(zfs, settings.stage)
        ensure_container(zfs, f"{settings.stage}/old")
        zfs.snapshot(f"{dataset}@{SEED_SNAPSHOT}", recursive=True)
        zfs.send_recv(["-R", f"{dataset}@{SEED_SNAPSHOT}"], encrypted_recv_args(settings, staged))
        zfs.unload_key(staged)
    return []


def free_snapshot_name(zfs: Zfs, snap: str, names: Sequence[str]) -> str:
    """Return the first ``<snap>-N`` that none of ``names`` has, to rename a stale ``snap`` aside to."""
    number = 1
    while any(zfs.exists(f"{name}@{snap}-{number}") for name in names):
        number += 1
    return f"{snap}-{number}"


def rename_aside_commands(zfs: Zfs, snap: str, names: Sequence[str], *, recursive: bool) -> list[str]:
    """Commands that rename ``snap`` aside on every one of ``names``, all to the same free name.

    Stale snapshots are renamed, never destroyed: the staged head still matches its stale snapshot,
    so the next incremental receive can start from it, while after a destroy recv refuses the head as
    modified. A renamed snapshot keeps its GUID, so recv skips it on the side that already has it.
    """
    aside = free_snapshot_name(zfs, snap, names)
    flag = "-r " if recursive else ""
    return [f"zfs rename {flag}{name}@{snap} @{aside}" for name in names]


def refuse_stale(zfs: Zfs, snap: str, names: Sequence[str], *, recursive: bool) -> None:
    """Refuse when any of ``names`` already has ``snap``, with the command that renames each one aside."""
    stale = [name for name in names if zfs.exists(f"{name}@{snap}")]
    if stale:
        commands = rename_aside_commands(zfs, snap, stale, recursive=recursive)
        lines = [
            f"{name}@{snap} already exists; rename it aside with: {command}"
            for name, command in zip(stale, commands, strict=True)
        ]
        raise MigrationError("\n".join(lines))


def catchup_staged(zfs: Zfs, settings: Settings, dataset: str) -> None:
    """Send what changed since the seed and verify the result; the caller guards the staged key."""
    staged = f"{settings.stage}/{leaf_of(dataset)}"
    if not zfs.exists(staged):
        msg = f"{staged} does not exist; run seed first"
        raise MigrationError(msg)
    if not zfs.exists(f"{dataset}@{SEED_SNAPSHOT}"):
        msg = f"{dataset}@{SEED_SNAPSHOT} does not exist; run seed first"
        raise MigrationError(msg)
    refuse_stale(zfs, FINAL_SNAPSHOT, [dataset, staged], recursive=True)
    load_stage_key(zfs, staged)
    zfs.snapshot(f"{dataset}@{FINAL_SNAPSHOT}", recursive=True)
    # Children created after the seed arrive as full streams, which stay unencrypted unless told to inherit.
    zfs.send_recv(
        ["-R", "-I", f"@{SEED_SNAPSHOT}", f"{dataset}@{FINAL_SNAPSHOT}"],
        ["-u", "-x", "encryption", staged],
    )
    if not snaps_match(zfs, dataset, staged, FINAL_SNAPSHOT):
        msg = f"{FINAL_SNAPSHOT} snapshots of {dataset} and {staged} differ"
        raise MigrationError(msg)
    check_encrypted_under(zfs, staged, staged)


def catchup(zfs: Zfs, settings: Settings, dataset: str) -> list[str]:
    """Bring the staged tree up to date, then unload its key."""
    refuse_nested(settings, dataset)
    staged = f"{settings.stage}/{leaf_of(dataset)}"
    with abort_guard(zfs, settings, staged):
        catchup_staged(zfs, settings, dataset)
        zfs.unload_key(staged)
    return []


def swap_into_place(zfs: Zfs, settings: Settings, dataset: str, staged: str, guard: AbortGuard) -> None:
    """Replace ``dataset`` by ``staged``, keeping the unencrypted original read-only under the stage."""
    old = f"{settings.stage}/old/{leaf_of(dataset)}"
    unmount_subtree(zfs, dataset)
    guard.phase = "canmount"
    # Volumes have no canmount and never mount; they just move along with the renames.
    for name in zfs.filesystems(dataset):
        if zfs.get("canmount", name) == "on":
            guard.canmount_on.append(name)
        zfs.set_prop(name, "canmount", "noauto")
    zfs.rename(dataset, old)
    guard.phase = "renamed-old"
    zfs.set_prop(old, "readonly", "on")
    zfs.rename(staged, dataset)
    guard.phase = "renamed-new"
    zfs.set_prop(dataset, "keylocation", settings.final_keylocation)
    for name in zfs.filesystems(dataset):
        if zfs.get("canmount", name) != "on":
            continue
        if zfs.get("mountpoint", name) in {"none", "legacy"}:
            continue
        if zfs.get("mounted", name) == "yes":
            continue
        zfs.mount(name)


def cutover(zfs: Zfs, settings: Settings, dataset: str) -> list[str]:
    """Swap the staged tree into place of ``dataset`` after a final catch-up."""
    if "/" not in dataset:
        msg = f"{dataset} is a pool root; cutover is not supported"
        raise MigrationError(msg)
    leaf = leaf_of(dataset)
    staged = f"{settings.stage}/{leaf}"
    refuse_cross_pool(settings, dataset, "cutover")
    refuse_nested(settings, dataset)
    if not zfs.exists(staged):
        msg = f"{staged} does not exist; run seed first"
        raise MigrationError(msg)
    # zfs rename -u refuses volumes, so the swap would stop half way; volumes inside a filesystem move with it.
    if zfs.get("type", dataset) == "volume":
        msg = f"{dataset} is a volume; cutover swaps filesystems only, with any volumes under them"
        raise MigrationError(msg)
    # The guard is armed before the load so an interrupt while the key loads still unloads it; the
    # recovery hints only apply once the source is about to be touched.
    with abort_guard(zfs, settings, staged, label="cutover") as guard:
        load_stage_key(zfs, staged)
        guard.source = dataset
        guard.leaf = leaf
        unmount_subtree(zfs, dataset)
        # A catch-up whose enc-final snapshots still match on both sides is not repeated.
        if not snaps_match(zfs, dataset, staged, FINAL_SNAPSHOT):
            catchup_staged(zfs, settings, dataset)
        zfs.pool_sync(pool_of(dataset))
        dirty = dirty_since(zfs, dataset, FINAL_SNAPSHOT)
        if dirty:
            raise MigrationError(
                "written since catchup:\n" + "\n".join(dirty),
                [
                    "rename the stale snapshots aside, then retry cutover:",
                    *(
                        f"  {command}"
                        for command in rename_aside_commands(zfs, FINAL_SNAPSHOT, [dataset, staged], recursive=True)
                    ),
                ],
            )
        swap_into_place(zfs, settings, dataset, staged, guard)
    return [
        f"verify the key with: zfs load-key -n {dataset}",
        f"cutover done; once verified, free the old copy with: zfs destroy -r {settings.stage}/old/{leaf}",
    ]
