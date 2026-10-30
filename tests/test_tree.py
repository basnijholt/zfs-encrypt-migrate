"""Tests for tree-seed, tree-move and tree-finish against a simulated zfs."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate.errors import Interrupted, MigrationError
from zfs_encrypt_migrate.migrate import Settings, cutover
from zfs_encrypt_migrate.tree import tree_finish, tree_move, tree_seed
from zfs_encrypt_migrate.zfs import Zfs

from .conftest import STAGE, FakeRunner, fail
from .helpers import fail_on, make
from .test_migrate import recv_args
from .zfs_sim import ZfsSim, staged_ready

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from zfs_encrypt_migrate.process import CommandResult

SOURCE = "tank/data/child"
STAGED = f"{STAGE}/data/child"
STAGED_ROOT = f"{STAGE}/data"
MOVED = [
    f"verify the key with: zfs load-key -n -L prompt {STAGED_ROOT}",
    f"moved {SOURCE}; once verified, free it with: zfs destroy -r {SOURCE}",
]
START_OVER = (f"  zfs destroy -r {SOURCE}@enc-tree", f"  zfs destroy -r {STAGED}")
MAY_BE_PARTIAL = (
    "tree-move aborted.",
    f"{SOURCE} may be unmounted; remount with: zfs mount -a",
    f"{STAGED} may hold a partial copy; remove it and the tree snapshots, then rerun tree-move:",
    *START_OVER,
)


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

    # The passphrase check comes first: once the source is destroyed, the staged copy is the only one.
    assert lines == MOVED
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
        (lambda sim: setattr(sim, "avail", 1050), "below floor 1000"),
    ],
    ids=["no-tree-snapshot", "no-source", "free-space"],
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


def start_over(sim: ZfsSim) -> None:
    """Apply what the two printed zfs destroy -r commands remove."""
    for name in (SOURCE, f"{SOURCE}/grand", STAGED, f"{STAGED}/grand"):
        sim.snapshots.pop(f"{name}@enc-tree")
    sim.datasets -= {STAGED, f"{STAGED}/grand"}


def test_tree_move_rerun_after_a_finished_move_reports_it_again(sim: ZfsSim, settings: Settings) -> None:
    """A complete, unchanged copy is reported as moved again after the same sync and written@ check as a move."""
    tree_seeded(sim)
    zfs, _ = make(sim)
    tree_move(zfs, settings, "tank/data", "child")
    sim.log.clear()

    lines = tree_move(zfs, settings, "tank/data", "child")

    assert lines == [f"{SOURCE} was already moved to {STAGED}", *MOVED]
    # The source is still unmounted, the key is never loaded and nothing is written.
    assert sim.ops() == [("zpool", "sync", "tank"), written(SOURCE), written(f"{SOURCE}/grand")]


def test_tree_move_rerun_after_writes_refuses_again_until_cleaned_up(sim: ZfsSim, settings: Settings) -> None:
    """A run refused for writes since the snapshot leaves a complete copy; the rerun refuses again, no destroy hint."""
    tree_seeded(sim)
    sim.prop(f"{SOURCE}/grand", "written@enc-tree", "4096")
    zfs, _ = make(sim)

    for _ in range(2):
        with pytest.raises(MigrationError) as info:
            tree_move(zfs, settings, "tank/data", "child")
        assert info.value.message == f"written since the tree snapshot:\n{SOURCE}/grand"
        assert info.value.hints == MAY_BE_PARTIAL

    start_over(sim)
    sim.props.pop((f"{SOURCE}/grand", "written@enc-tree"))
    assert tree_move(zfs, settings, "tank/data", "child") == MOVED


def test_tree_move_rerun_after_a_remount_unmounts_and_refuses_writes(sim: ZfsSim, settings: Settings) -> None:
    """A source mounted again after the move, by a reboot or zfs mount -a, is unmounted and checked for writes."""
    tree_seeded(sim)
    zfs, _ = make(sim)
    tree_move(zfs, settings, "tank/data", "child")
    sim.prop(SOURCE, "mounted", "yes")
    sim.prop(SOURCE, "written@enc-tree", "4096")
    sim.log.clear()

    with pytest.raises(MigrationError, match="written since the tree snapshot") as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert info.value.hints == MAY_BE_PARTIAL
    assert sim.ops()[:2] == [("zfs", "unmount", SOURCE), ("zpool", "sync", "tank")]
    assert not [call for call in sim.ops() if call[1] in {"load-key", "destroy"}]


def test_tree_move_rerun_with_busy_source_hints_only_the_remount(sim: ZfsSim, settings: Settings) -> None:
    """A busy remounted source refuses like a move does: stop the writer and rerun, nothing to clean up yet."""
    tree_seeded(sim)
    zfs, _ = make(sim)
    tree_move(zfs, settings, "tank/data", "child")
    sim.prop(SOURCE, "mounted", "yes")
    sim.busy.add(SOURCE)

    with pytest.raises(MigrationError, match=f"cannot unmount {SOURCE}") as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert info.value.hints == ("tree-move aborted.", f"{SOURCE} may be unmounted; remount with: zfs mount -a")


def test_tree_move_refuses_a_recreated_source_without_destroy_hints(sim: ZfsSim, settings: Settings) -> None:
    """A source without @enc-tree next to a staged child was created after a finished move: keep the staged copy."""
    tree_seeded(sim)
    zfs, _ = make(sim)
    tree_move(zfs, settings, "tank/data", "child")
    # The operator destroyed the source as told, then something (a backup push, say) created it again.
    sim.datasets.discard(f"{SOURCE}/grand")
    for name in (SOURCE, f"{SOURCE}/grand"):
        sim.snapshots.pop(f"{name}@enc-tree")
    sim.log.clear()

    with pytest.raises(MigrationError) as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert info.value.message == f"{STAGED} already exists but {SOURCE} has no @enc-tree; {SOURCE} looks recreated"
    assert info.value.hints == (
        f"keep {STAGED}: it may be the only copy of the original data from an earlier move.",
        f"Resolve this by hand, for example by renaming the new {SOURCE} aside with zfs rename.",
    )
    assert not [hint for hint in info.value.hints if "destroy" in hint]
    assert sim.ops() == []


@pytest.mark.parametrize(
    "interrupted",
    [
        lambda sim: (sim.datasets.remove(f"{STAGED}/grand"), sim.snapshots.pop(f"{STAGED}/grand@enc-tree")),
        lambda sim: sim.snapshots.pop(f"{STAGED}@enc-tree"),
        lambda sim: sim.prop(f"{STAGED}/grand", "encryptionroot", "-"),
    ],
    ids=["missing-descendant", "older-head", "not-encrypted"],
)
def test_tree_move_refuses_a_partial_copy_with_the_cleanup(
    sim: ZfsSim,
    settings: Settings,
    interrupted: Callable[[ZfsSim], object],
) -> None:
    """An incomplete copy left by an interrupted run is refused with its cleanup, never reported as moved."""
    tree_seeded(sim)
    zfs, _ = make(sim)
    tree_move(zfs, settings, "tank/data", "child")
    interrupted(sim)
    sim.log.clear()

    with pytest.raises(MigrationError) as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert info.value.message == (
        f"{STAGED} already exists but is not a complete copy of {SOURCE}; a tree-move was interrupted or failed"
    )
    assert info.value.hints == (
        "remove the partial copy and the tree snapshots, then rerun tree-move:",
        *START_OVER,
    )
    assert sim.ops() == []


def test_tree_move_reruns_once_the_partial_copy_is_removed(sim: ZfsSim, settings: Settings) -> None:
    """The printed cleanup is all a rerun needs."""
    tree_seeded(sim)
    zfs, _ = make(sim)
    tree_move(zfs, settings, "tank/data", "child")
    start_over(sim)

    assert tree_move(zfs, settings, "tank/data", "child") == MOVED


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

    # Only the staged copy and the tree snapshots are to be destroyed, never the source itself.
    assert info.value.hints == MAY_BE_PARTIAL
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
    assert info.value.hints == MAY_BE_PARTIAL
    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls
    assert not [c for c in runner.calls if c[1] == "destroy"]


def test_tree_move_failed_receive_unloads_key(sim: ZfsSim, settings: Settings) -> None:
    """A failing receive unloads the staged key and names the child in the hint."""
    tree_seeded(sim)
    zfs, runner = make(sim, fail_on("zfs", "recv"))

    with pytest.raises(MigrationError, match="injected") as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert ("zfs", "unload-key", "-r", STAGED_ROOT) in runner.calls
    assert info.value.hints == MAY_BE_PARTIAL


def test_tree_move_failed_snapshot_hints_no_cleanup(sim: ZfsSim, settings: Settings) -> None:
    """Before the tree snapshot exists there is nothing to remove, so only the remount is hinted."""
    tree_seeded(sim)
    zfs, _ = make(sim, fail_on("zfs", "snapshot"))

    with pytest.raises(MigrationError, match="injected") as info:
        tree_move(zfs, settings, "tank/data", "child")

    assert info.value.hints == ("tree-move aborted.", f"{SOURCE} may be unmounted; remount with: zfs mount -a")


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
    assert info.value.hints == MAY_BE_PARTIAL


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

    assert lines == [
        "verify the key with: zfs load-key -n tank/data",
        f"tree-finish done; once verified, free the old copy with: zfs destroy -r {STAGE}/old/data",
    ]
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


SOURCE_STALE = "tank/data@enc-tree-final already exists; rename it aside with: zfs rename tank/data@enc-tree-final"
STAGED_STALE = (
    f"{STAGED_ROOT}@enc-tree-final already exists; rename it aside with: zfs rename {STAGED_ROOT}@enc-tree-final"
)


@pytest.mark.parametrize(
    ("stale", "taken", "expected"),
    [
        (["tank/data@enc-tree-final"], [], [f"{SOURCE_STALE} @enc-tree-final-1"]),
        ([f"{STAGED_ROOT}@enc-tree-final"], [], [f"{STAGED_STALE} @enc-tree-final-1"]),
        (
            ["tank/data@enc-tree-final", f"{STAGED_ROOT}@enc-tree-final"],
            [],
            [f"{SOURCE_STALE} @enc-tree-final-1", f"{STAGED_STALE} @enc-tree-final-1"],
        ),
        (
            ["tank/data@enc-tree-final", f"{STAGED_ROOT}@enc-tree-final"],
            [f"{STAGED_ROOT}@enc-tree-final-1"],
            [f"{SOURCE_STALE} @enc-tree-final-2", f"{STAGED_STALE} @enc-tree-final-2"],
        ),
    ],
    ids=["source", "staged", "both", "next-free-suffix"],
)
def test_tree_finish_refuses_stale_final(
    sim: ZfsSim,
    settings: Settings,
    stale: list[str],
    taken: list[str],
    expected: list[str],
) -> None:
    """A leftover enc-tree-final is refused with non-recursive renames to a free suffix, before any write."""
    finish_ready(sim)
    for name in [*stale, *taken]:
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
    """Writes after the final snapshot list the datasets and the renames that set both finals aside."""
    finish_ready(sim)
    sim.prop("tank/data", "written@enc-tree-final", "4096")
    zfs, runner = make(sim)

    with pytest.raises(MigrationError) as info:
        tree_finish(zfs, settings, "tank/data")

    assert info.value.message == "written since the final snapshot:\ntank/data"
    assert info.value.hints == (
        "rename the stale snapshots aside, then retry tree-finish:",
        "  zfs rename tank/data@enc-tree-final @enc-tree-final-1",
        f"  zfs rename {STAGED_ROOT}@enc-tree-final @enc-tree-final-1",
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


# --- the guard is armed before the key loads ---


def load_key_then(sim: ZfsSim, fault: BaseException | None) -> Callable[[Sequence[str]], CommandResult | None]:
    """Answer load-key like zfs (the key loads), then fail or raise as the step right after it."""

    def responder(args: Sequence[str]) -> CommandResult | None:
        if args[1] != "load-key":
            return None
        sim.answer(args)
        if fault is not None:
            raise fault
        return fail("injected\n")

    return responder


def _cutover_ready(sim: ZfsSim) -> None:
    staged_ready(sim, final=True)


def _tree_move_ready(sim: ZfsSim) -> None:
    tree_seeded(sim)


def _tree_finish_ready(sim: ZfsSim) -> None:
    finish_ready(sim)


def _cutover(zfs: Zfs, settings: Settings) -> list[str]:
    return cutover(zfs, settings, "tank/data")


def _tree_move(zfs: Zfs, settings: Settings) -> list[str]:
    return tree_move(zfs, settings, "tank/data", "child")


def _tree_finish(zfs: Zfs, settings: Settings) -> list[str]:
    return tree_finish(zfs, settings, "tank/data")


GUARDED = pytest.mark.parametrize(
    ("ready", "command"),
    [(_cutover_ready, _cutover), (_tree_move_ready, _tree_move), (_tree_finish_ready, _tree_finish)],
    ids=["cutover", "tree-move", "tree-finish"],
)


@GUARDED
def test_failure_right_after_load_key_unloads_it(
    sim: ZfsSim,
    settings: Settings,
    ready: Callable[[ZfsSim], None],
    command: Callable[[Zfs, Settings], list[str]],
) -> None:
    """A load-key that loaded the key but failed leaves it unloaded and prints no recovery hint."""
    ready(sim)
    zfs, _ = make(sim, load_key_then(sim, None))

    with pytest.raises(MigrationError, match="injected") as info:
        command(zfs, settings)

    assert sim.props[STAGED_ROOT, "keystatus"] == "unavailable"
    assert not [hint for hint in info.value.hints if "aborted." in hint]


@GUARDED
def test_interrupt_right_after_load_key_unloads_it(
    sim: ZfsSim,
    settings: Settings,
    ready: Callable[[ZfsSim], None],
    command: Callable[[Zfs, Settings], list[str]],
) -> None:
    """Ctrl-C while the key loads still unloads it."""
    ready(sim)
    zfs, _ = make(sim, load_key_then(sim, KeyboardInterrupt()))

    with pytest.raises(Interrupted) as info:
        command(zfs, settings)

    assert sim.props[STAGED_ROOT, "keystatus"] == "unavailable"
    assert not [hint for hint in info.value.hints if "aborted." in hint]


# --- cross-pool ---


def test_tree_seed_refuses_dataset_on_other_pool_than_stage(settings: Settings) -> None:
    """tree-finish could never rename the tree into place, so tree-seed refuses before any command."""
    zfs, runner = make(ZfsSim(["ssd", "ssd/data"]))

    with pytest.raises(MigrationError, match="ssd/data is on pool ssd but the stage is on tank; tree-seed cannot"):
        tree_seed(zfs, settings, "ssd/data")
    assert runner.calls == []


def test_tree_move_refuses_dataset_on_other_pool_than_stage(settings: Settings) -> None:
    """tree-move refuses before any command, so no child is ever moved for a tree that cannot finish."""
    zfs, runner = make(ZfsSim(["ssd", "ssd/data", "ssd/data/child"]))

    with pytest.raises(MigrationError, match="ssd/data is on pool ssd but the stage is on tank; tree-move cannot"):
        tree_move(zfs, settings, "ssd/data", "child")
    assert runner.calls == []


# --- stage and dataset overlap ---


@pytest.mark.parametrize(
    ("stage", "dataset"),
    [("tank/data", "tank/data"), ("tank/data/stage", "tank/data"), ("tank/stage", "tank/stage/old")],
    ids=["same", "stage-inside", "dataset-inside"],
)
@pytest.mark.parametrize(
    "command",
    [
        tree_seed,
        lambda zfs, settings, dataset: tree_move(zfs, settings, dataset, "child"),
        tree_finish,
    ],
    ids=["tree-seed", "tree-move", "tree-finish"],
)
def test_tree_commands_refuse_a_stage_overlapping_the_dataset(
    sim: ZfsSim,
    settings: Settings,
    stage: str,
    dataset: str,
    command: Callable[[Zfs, Settings, str], list[str]],
) -> None:
    """The copy would land inside its own source, or the swap rename the stage into itself: refuse before zfs runs."""
    zfs, runner = make(sim)

    with pytest.raises(MigrationError, match=f"{dataset} overlaps the stage {stage}; neither may be or contain"):
        command(zfs, replace(settings, stage=stage), dataset)
    assert runner.calls == []
