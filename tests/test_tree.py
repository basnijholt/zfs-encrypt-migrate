"""Tests for tree-seed, tree-move and tree-finish against a simulated zfs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate.errors import Interrupted, MigrationError
from zfs_encrypt_migrate.migrate import Settings
from zfs_encrypt_migrate.tree import tree_finish, tree_move, tree_seed
from zfs_encrypt_migrate.zfs import Zfs

from .conftest import STAGE, FakeRunner
from .helpers import fail_on, make
from .test_migrate import recv_args
from .zfs_sim import ZfsSim

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from zfs_encrypt_migrate.process import CommandResult

SOURCE = "tank/data/child"
STAGED = f"{STAGE}/data/child"
STAGED_ROOT = f"{STAGE}/data"


def written(name: str, snap: str = "enc-tree") -> tuple[str, ...]:
    """Return the written@ read that the tree commands issue."""
    return ("zfs", "get", "-H", "-p", "-o", "value", f"written@{snap}", name)


@pytest.fixture
def sim() -> ZfsSim:
    """Build a pool with a parent, a child and a grandchild, all mounted."""
    simulator = ZfsSim(["tank", "tank/data", SOURCE, f"{SOURCE}/grand"])
    simulator.roots = ["tank/data", STAGED_ROOT]
    for name in simulator.datasets - {"tank"}:
        simulator.prop(name, "mounted", "yes")
    return simulator


def tree_seeded(sim: ZfsSim) -> None:
    """Set up the state after tree-seed: the parent alone is staged, encrypted, key unloaded."""
    sim.datasets |= {STAGE, f"{STAGE}/old", STAGED_ROOT}
    sim.snapshot("tank/data@enc-tree")
    sim.snapshot(f"{STAGED_ROOT}@enc-tree")
    sim.prop(STAGED_ROOT, "encryptionroot", STAGED_ROOT)
    sim.prop(STAGED_ROOT, "encryption", "aes-256-gcm")
    sim.prop(STAGED_ROOT, "keystatus", "unavailable")


def children_moved(sim: ZfsSim) -> None:
    """Set up the state before tree-finish: the children are staged and destroyed at the source."""
    sim.datasets -= {SOURCE, f"{SOURCE}/grand"}
    sim.datasets |= {STAGED, f"{STAGED}/grand"}
    for name in (STAGED, f"{STAGED}/grand"):
        sim.prop(name, "encryptionroot", STAGED_ROOT)
        sim.prop(name, "encryption", "aes-256-gcm")
        sim.prop(name, "keystatus", "unavailable")


# --- tree-seed ---


def test_tree_seed_exact_commands(sim: ZfsSim, settings: Settings, key_file: Path) -> None:
    """tree-seed snapshots the parent alone, sends it with properties and unloads the key."""
    zfs, runner = make(sim)

    assert tree_seed(zfs, settings, "tank/data") == []

    assert runner.pipelines == [(("zfs", "send", "-p", "tank/data@enc-tree"), recv_args(key_file, STAGED_ROOT))]
    assert sim.ops() == [
        ("zfs", "create", "-o", "canmount=off", STAGE),
        ("zfs", "create", "-o", "canmount=off", f"{STAGE}/old"),
        ("zfs", "snapshot", "tank/data@enc-tree"),
        ("zfs", "send", "-p", "tank/data@enc-tree"),
        recv_args(key_file, STAGED_ROOT),
        ("zfs", "unload-key", "-r", STAGED_ROOT),
    ]
    # The stream is not recursive, so the children are not staged.
    assert STAGED not in sim.datasets
    assert sim.props[STAGED_ROOT, "keystatus"] == "unavailable"


def test_tree_seed_sizes_the_parent_without_its_children(sim: ZfsSim, settings: Settings) -> None:
    """The free-space check reads usedbydataset and usedbysnapshots, not used."""
    zfs, runner = make(sim)

    tree_seed(zfs, settings, "tank/data")

    assert ("zfs", "get", "-H", "-p", "-o", "value", "usedbydataset,usedbysnapshots", "tank/data") in runner.calls


def test_tree_seed_refuses_pool_root(settings: Settings) -> None:
    """A pool root is refused before any command runs."""
    zfs, runner = make(ZfsSim(["tank"]))

    with pytest.raises(MigrationError, match="tank is a pool root; the tree move is not supported"):
        tree_seed(zfs, settings, "tank")
    assert runner.calls == []


def test_tree_seed_refuses_below_free_space_floor_before_any_write(sim: ZfsSim, settings: Settings) -> None:
    """The parent's own size is checked against the floor; nothing is written."""
    sim.avail = 1500
    sim.prop("tank/data", "usedbydataset", "400")
    sim.prop("tank/data", "usedbysnapshots", "200")
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match="below floor 1000"):
        tree_seed(zfs, settings, "tank/data")
    assert sim.ops() == []


def test_tree_seed_refuses_existing_staged_before_any_write(sim: ZfsSim, settings: Settings) -> None:
    """A staged tree left by an earlier tree-seed counts as a migration in flight."""
    tree_seeded(sim)
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match="a migration is already in flight"):
        tree_seed(zfs, settings, "tank/data")
    assert sim.ops() == []


def test_tree_seed_failure_unloads_key(sim: ZfsSim, settings: Settings) -> None:
    """A failed unload after the receive still surfaces, with the manual unload hint."""
    zfs, _ = make(sim, fail_on("zfs", "unload-key"))

    with pytest.raises(MigrationError, match="injected") as info:
        tree_seed(zfs, settings, "tank/data")

    assert f"could not unload key; run: zfs unload-key -r {STAGED_ROOT}" in info.value.hints


def test_tree_seed_interrupt_unloads_key(sim: ZfsSim, settings: Settings) -> None:
    """Ctrl-C mid-pipeline unloads the staged key."""

    class Interrupting(FakeRunner):
        def pipeline(self, first: Sequence[str], second: Sequence[str]) -> tuple[CommandResult, CommandResult]:
            self.pipelines.append((tuple(first), tuple(second)))
            sim.datasets.add(second[-1])
            sim.prop(second[-1], "keystatus", "available")
            raise KeyboardInterrupt

    runner = Interrupting(sim.answer)

    with pytest.raises(Interrupted):
        tree_seed(Zfs(runner), settings, "tank/data")

    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls


# --- tree-move ---


def test_tree_move_exact_commands(sim: ZfsSim, settings: Settings) -> None:
    """tree-move loads the key, unmounts, snapshots, sends, syncs, then reads written@, then unloads."""
    tree_seeded(sim)
    zfs, runner = make(sim)

    lines = tree_move(zfs, settings, "tank/data", "child")

    assert lines == [f"moved {SOURCE}; once verified, free it with: zfs destroy -r {SOURCE}"]
    assert runner.pipelines == [
        (("zfs", "send", "-R", f"{SOURCE}@enc-tree"), ("zfs", "recv", "-u", "-x", "encryption", STAGED)),
    ]
    assert sim.ops() == [
        ("zfs", "load-key", STAGED_ROOT),
        ("zfs", "unmount", f"{SOURCE}/grand"),
        ("zfs", "unmount", SOURCE),
        ("zfs", "snapshot", "-r", f"{SOURCE}@enc-tree"),
        ("zfs", "send", "-R", f"{SOURCE}@enc-tree"),
        ("zfs", "recv", "-u", "-x", "encryption", STAGED),
        ("zpool", "sync", "tank"),
        written(SOURCE),
        written(f"{SOURCE}/grand"),
        ("zfs", "unload-key", "-r", STAGED_ROOT),
    ]
    assert sim.props[f"{STAGED}/grand", "encryptionroot"] == STAGED_ROOT


def test_tree_move_never_destroys(sim: ZfsSim, settings: Settings) -> None:
    """No command of a full tree-move is a destroy."""
    tree_seeded(sim)
    zfs, runner = make(sim)

    tree_move(zfs, settings, "tank/data", "child")

    assert not [c for c in runner.calls if c[1] == "destroy"]


def test_tree_move_sizes_the_child_by_used(sim: ZfsSim, settings: Settings) -> None:
    """The free-space check reads used of the child, not of the parent."""
    tree_seeded(sim)
    zfs, runner = make(sim)

    tree_move(zfs, settings, "tank/data", "child")

    assert ("zfs", "get", "-H", "-p", "-o", "value", "used", SOURCE) in runner.calls


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        (
            lambda sim: sim.snapshots.pop(f"{STAGED_ROOT}@enc-tree"),
            f"{STAGED_ROOT}@enc-tree does not exist; run tree-seed first",
        ),
        (lambda sim: sim.datasets.remove(SOURCE), f"dataset {SOURCE} does not exist"),
        (lambda sim: sim.datasets.add(STAGED), f"{STAGED} already exists"),
        (lambda sim: setattr(sim, "avail", 1050), "below floor 1000"),
    ],
    ids=["no-tree-snapshot", "no-source", "staged-exists", "free-space"],
)
def test_tree_move_refusals_write_nothing(sim: ZfsSim, settings: Settings, prepare: object, message: str) -> None:
    """Every refusal happens before the key is loaded or anything is unmounted."""
    tree_seeded(sim)
    prepare(sim)  # type: ignore[operator]
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match=message) as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert sim.ops() == []
    assert info.value.hints == ()


def test_tree_move_busy_unmount_hints_remount_of_the_child(sim: ZfsSim, settings: Settings) -> None:
    """A busy child refuses, unloads the key and hints tree-move with the child to remount."""
    tree_seeded(sim)
    sim.busy.add(SOURCE)
    zfs, runner = make(sim)

    with pytest.raises(MigrationError, match=f"cannot unmount {SOURCE}") as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert info.value.hints == ("tree-move aborted.", f"{SOURCE} may be unmounted; remount with: zfs mount -a")
    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls
    assert [c for c in runner.calls if c[1] == "snapshot"] == []


def test_tree_move_refuses_guid_mismatch(sim: ZfsSim, settings: Settings) -> None:
    """Diverging snapshots refuse and the key is unloaded."""
    tree_seeded(sim)
    zfs, runner = make(
        sim,
        after=lambda args: sim.snapshots.update({f"{STAGED}/grand@enc-tree": "other"}) if args[1] == "recv" else None,
    )

    with pytest.raises(MigrationError, match=f"enc-tree snapshots of {SOURCE} and {STAGED} differ"):
        tree_move(zfs, settings, "tank/data", "child")

    assert ("zpool", "sync", "tank") not in runner.calls
    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls


def test_tree_move_refuses_unencrypted_descendant_before_the_destroy_hint(sim: ZfsSim, settings: Settings) -> None:
    """A descendant outside the encryption root is refused, even when the source also changed."""
    tree_seeded(sim)
    sim.prop(SOURCE, "written@enc-tree", "4096")
    zfs, runner = make(
        sim,
        after=lambda args: sim.prop(f"{STAGED}/grand", "encryptionroot", "-") if args[1] == "recv" else None,
    )

    with pytest.raises(MigrationError, match=f"{STAGED}/grand is not encrypted under {STAGED_ROOT}") as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert not [hint for hint in info.value.hints if "destroy" in hint]
    assert ("zpool", "sync", "tank") not in runner.calls


def test_sim_receive_without_x_encryption_leaves_new_children_unencrypted(sim: ZfsSim) -> None:
    """The simulator models why -x encryption is needed: without it a new child is not encrypted."""
    tree_seeded(sim)
    sim.prop(STAGED_ROOT, "keystatus", "available")
    sim.snapshot(f"{SOURCE}@enc-tree")

    sim.answer(["zfs", "send", "-R", f"{SOURCE}@enc-tree"])
    sim.answer(["zfs", "recv", "-u", STAGED])

    assert sim.props[STAGED, "encryptionroot"] == "-"


def test_sim_rename_carries_snapshots_and_properties(sim: ZfsSim) -> None:
    """A rename moves the snapshots and properties of the dataset and its descendants."""
    sim.snapshot(f"{SOURCE}/grand@s")
    sim.prop(f"{SOURCE}/grand", "readonly", "on")

    sim.answer(["zfs", "rename", "-u", SOURCE, "tank/other"])

    assert "tank/other/grand@s" in sim.snapshots
    assert f"{SOURCE}/grand@s" not in sim.snapshots
    assert sim.props["tank/other/grand", "readonly"] == "on"


def test_tree_move_refuses_writes_since_the_tree_snapshot(sim: ZfsSim, settings: Settings) -> None:
    """Writes after the snapshot list the datasets and both destroy hints; the key is unloaded."""
    tree_seeded(sim)
    sim.prop(f"{SOURCE}/grand", "written@enc-tree", "4096")
    zfs, runner = make(sim)

    with pytest.raises(MigrationError) as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert info.value.message == f"written since the tree snapshot:\n{SOURCE}/grand"
    assert info.value.hints == (
        "drop the stale snapshots and the partial copy, then retry tree-move:",
        f"  zfs destroy -r {SOURCE}@enc-tree",
        f"  zfs destroy -r {STAGED}",
        "tree-move aborted.",
        f"{SOURCE} may be unmounted; remount with: zfs mount -a",
    )
    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls
    assert not [c for c in runner.calls if c[1] == "destroy"]


def test_tree_move_failed_receive_unloads_key(sim: ZfsSim, settings: Settings) -> None:
    """A failing receive unloads the staged key and names the child in the hint."""
    tree_seeded(sim)
    zfs, runner = make(sim, fail_on("zfs", "recv"))

    with pytest.raises(MigrationError, match="injected") as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls
    assert info.value.hints[0] == "tree-move aborted."


def test_tree_move_interrupt_unloads_key(sim: ZfsSim, settings: Settings) -> None:
    """Ctrl-C mid-pipeline unloads the staged key and prints the tree-move hints."""
    tree_seeded(sim)

    class Interrupting(FakeRunner):
        def pipeline(self, first: Sequence[str], second: Sequence[str]) -> tuple[CommandResult, CommandResult]:
            self.pipelines.append((tuple(first), tuple(second)))
            raise KeyboardInterrupt

    runner = Interrupting(sim.answer)

    with pytest.raises(Interrupted) as info:
        tree_move(Zfs(runner), settings, "tank/data", "child")

    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls
    assert info.value.hints[0] == "tree-move aborted."


def test_tree_move_skips_load_when_key_loaded(sim: ZfsSim, settings: Settings) -> None:
    """An already loaded key is not loaded again."""
    tree_seeded(sim)
    sim.prop(STAGED_ROOT, "keystatus", "available")
    zfs, runner = make(sim)

    tree_move(zfs, settings, "tank/data", "child")

    assert not [c for c in runner.calls if c[1] == "load-key"]


# --- tree-finish ---


def finish_ready(sim: ZfsSim) -> None:
    """Set up the state before tree-finish."""
    tree_seeded(sim)
    children_moved(sim)


def test_tree_finish_exact_commands(sim: ZfsSim, settings: Settings) -> None:
    """tree-finish catches up the parent alone, syncs, reads written@, then swaps; the moved children come along."""
    finish_ready(sim)
    zfs, runner = make(sim)

    lines = tree_finish(zfs, settings, "tank/data")

    assert lines == [f"tree-finish done; once verified, free the old copy with: zfs destroy -r {STAGE}/old/data"]
    assert runner.pipelines == [
        (("zfs", "send", "-p", "-I", "@enc-tree", "tank/data@enc-tree-final"), ("zfs", "recv", "-u", STAGED_ROOT)),
    ]
    assert sim.ops() == [
        ("zfs", "load-key", STAGED_ROOT),
        ("zfs", "unmount", "tank/data"),
        ("zfs", "snapshot", "tank/data@enc-tree-final"),
        ("zfs", "send", "-p", "-I", "@enc-tree", "tank/data@enc-tree-final"),
        ("zfs", "recv", "-u", STAGED_ROOT),
        ("zpool", "sync", "tank"),
        written("tank/data", "enc-tree-final"),
        ("zfs", "set", "canmount=noauto", "tank/data"),
        ("zfs", "rename", "-u", "tank/data", f"{STAGE}/old/data"),
        ("zfs", "set", "readonly=on", f"{STAGE}/old/data"),
        ("zfs", "rename", "-u", STAGED_ROOT, "tank/data"),
        ("zfs", "set", "keylocation=prompt", "tank/data"),
        ("zfs", "mount", "tank/data"),
        ("zfs", "mount", SOURCE),
        ("zfs", "mount", f"{SOURCE}/grand"),
    ]


def test_tree_finish_uses_final_keylocation_setting(sim: ZfsSim, key_file: Path) -> None:
    """The final keylocation reaches the swap."""
    finish_ready(sim)
    zfs, runner = make(sim)

    tree_finish(zfs, Settings(STAGE, key_file, 1000, "passphrase", "file:///etc/key"), "tank/data")

    assert ("zfs", "set", "keylocation=file:///etc/key", "tank/data") in runner.calls


def test_tree_finish_never_destroys_and_leaves_key_alone(sim: ZfsSim, settings: Settings) -> None:
    """A successful tree-finish never destroys and issues no unload."""
    finish_ready(sim)
    zfs, runner = make(sim)

    tree_finish(zfs, settings, "tank/data")

    assert not [c for c in runner.calls if c[1] in {"destroy", "unload-key"}]


def test_tree_finish_refuses_pool_root(settings: Settings) -> None:
    """A pool root is refused before any command runs."""
    zfs, runner = make(ZfsSim(["tank"]))

    with pytest.raises(MigrationError, match="tank is a pool root; the tree move is not supported"):
        tree_finish(zfs, settings, "tank")
    assert runner.calls == []


def test_tree_finish_refuses_dataset_on_other_pool_than_stage(settings: Settings) -> None:
    """The swap renames, which cannot cross pools; refuse before anything runs."""
    zfs, runner = make(ZfsSim(["ssd", "ssd/data"]))

    with pytest.raises(MigrationError, match="ssd/data is on pool ssd but the stage is on tank"):
        tree_finish(zfs, settings, "ssd/data")
    assert runner.calls == []


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        (
            lambda sim: sim.snapshots.pop(f"{STAGED_ROOT}@enc-tree"),
            f"{STAGED_ROOT}@enc-tree does not exist; run tree-seed first",
        ),
        (lambda sim: sim.snapshots.pop("tank/data@enc-tree"), "tank/data@enc-tree does not exist; run tree-seed first"),
        (
            lambda sim: sim.datasets.add(SOURCE),
            "children remain under tank/data; move each with tree-move and destroy it first",
        ),
        (
            lambda sim: sim.datasets.add("tank/data/vol"),
            "children remain under tank/data; move each with tree-move and destroy it first",
        ),
    ],
    ids=["no-staged-snapshot", "no-source-snapshot", "child-remains", "volume-remains"],
)
def test_tree_finish_refusals_write_nothing(sim: ZfsSim, settings: Settings, prepare: object, message: str) -> None:
    """Every refusal happens before the key is loaded or anything is unmounted."""
    finish_ready(sim)
    prepare(sim)  # type: ignore[operator]
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match=message) as info:
        tree_finish(zfs, settings, "tank/data")

    assert sim.ops() == []
    assert info.value.hints == ()


def test_tree_finish_lists_direct_children_only(sim: ZfsSim, settings: Settings) -> None:
    """The children check asks zfs for depth 1 only."""
    finish_ready(sim)
    zfs, runner = make(sim)

    tree_finish(zfs, settings, "tank/data")

    assert ("zfs", "list", "-rH", "-t", "filesystem,volume", "-o", "name", "-d", "1", "tank/data") in runner.calls


@pytest.mark.parametrize(
    ("stale", "expected"),
    [
        (
            ["tank/data@enc-tree-final"],
            ["tank/data@enc-tree-final already exists; drop it with: zfs destroy tank/data@enc-tree-final"],
        ),
        (
            [f"{STAGED_ROOT}@enc-tree-final"],
            [f"{STAGED_ROOT}@enc-tree-final already exists; drop it with: zfs destroy {STAGED_ROOT}@enc-tree-final"],
        ),
        (
            ["tank/data@enc-tree-final", f"{STAGED_ROOT}@enc-tree-final"],
            [
                "tank/data@enc-tree-final already exists; drop it with: zfs destroy tank/data@enc-tree-final",
                f"{STAGED_ROOT}@enc-tree-final already exists; drop it with: zfs destroy {STAGED_ROOT}@enc-tree-final",
            ],
        ),
    ],
    ids=["source", "staged", "both"],
)
def test_tree_finish_refuses_stale_final(
    sim: ZfsSim,
    settings: Settings,
    stale: list[str],
    expected: list[str],
) -> None:
    """A leftover enc-tree-final on either side is refused with its destroy hint, before any write."""
    finish_ready(sim)
    for name in stale:
        sim.snapshot(name)
    zfs, _ = make(sim)

    with pytest.raises(MigrationError) as info:
        tree_finish(zfs, settings, "tank/data")

    assert info.value.message == "\n".join(expected)
    assert sim.ops() == []


def test_tree_finish_busy_unmount_uses_its_own_label(sim: ZfsSim, settings: Settings) -> None:
    """A busy dataset refuses; the hint names tree-finish, not cutover."""
    finish_ready(sim)
    sim.busy.add("tank/data")
    zfs, runner = make(sim)

    with pytest.raises(MigrationError, match="cannot unmount tank/data") as info:
        tree_finish(zfs, settings, "tank/data")

    assert info.value.hints == ("tree-finish aborted.", "tank/data may be unmounted; remount with: zfs mount -a")
    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls


def test_tree_finish_refuses_guid_mismatch(sim: ZfsSim, settings: Settings) -> None:
    """Diverging final snapshots refuse before the sync and the swap."""
    finish_ready(sim)
    zfs, runner = make(
        sim,
        after=lambda args: (
            sim.snapshots.update({f"{STAGED_ROOT}@enc-tree-final": "other"}) if args[1] == "recv" else None
        ),
    )

    with pytest.raises(MigrationError, match=f"enc-tree-final snapshots of tank/data and {STAGED_ROOT} differ"):
        tree_finish(zfs, settings, "tank/data")

    assert ("zpool", "sync", "tank") not in runner.calls
    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls


def test_tree_finish_refuses_writes_since_the_final_snapshot(sim: ZfsSim, settings: Settings) -> None:
    """Writes after the final snapshot list the datasets and both destroy hints; nothing is swapped."""
    finish_ready(sim)
    sim.prop("tank/data", "written@enc-tree-final", "4096")
    zfs, runner = make(sim)

    with pytest.raises(MigrationError) as info:
        tree_finish(zfs, settings, "tank/data")

    assert info.value.message == "written since the final snapshot:\ntank/data"
    assert info.value.hints == (
        "drop the stale snapshots, then retry tree-finish:",
        "  zfs destroy tank/data@enc-tree-final",
        f"  zfs destroy {STAGED_ROOT}@enc-tree-final",
        "tree-finish aborted.",
        "tank/data may be unmounted; remount with: zfs mount -a",
    )
    assert ("zpool", "sync", "tank") in runner.calls
    assert not [c for c in runner.calls if c[1] in {"rename", "destroy"}]
    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls


def test_tree_finish_swap_failure_hints_use_phase(sim: ZfsSim, settings: Settings) -> None:
    """A failure in the swap gets the swap's phase hints, not the unmounted-only hint."""
    finish_ready(sim)
    zfs, _ = make(sim, fail_on("zfs", "rename", "-u", "tank/data"))

    with pytest.raises(MigrationError, match="injected") as info:
        tree_finish(zfs, settings, "tank/data")

    assert info.value.hints == (
        "tree-finish aborted.",
        "tank/data is unmounted and canmount may be noauto; restore with:",
        "  zfs set canmount=on tank/data",
        "  zfs mount -a",
    )


def test_tree_finish_interrupt_unloads_key(sim: ZfsSim, settings: Settings) -> None:
    """Ctrl-C mid-pipeline unloads the staged key and prints the tree-finish hints."""
    finish_ready(sim)

    class Interrupting(FakeRunner):
        def pipeline(self, first: Sequence[str], second: Sequence[str]) -> tuple[CommandResult, CommandResult]:
            self.pipelines.append((tuple(first), tuple(second)))
            raise KeyboardInterrupt

    runner = Interrupting(sim.answer)

    with pytest.raises(Interrupted) as info:
        tree_finish(Zfs(runner), settings, "tank/data")

    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls
    assert info.value.hints[0] == "tree-finish aborted."
