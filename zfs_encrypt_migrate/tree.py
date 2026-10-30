"""Move a parent with many children in steps: the parent alone, each child subtree, then the parent's last changes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import MigrationError
from .migrate import (
    abort_guard,
    check_encrypted_under,
    check_free_space,
    dirty_since,
    encrypted_recv_args,
    ensure_container,
    leaf_of,
    load_stage_key,
    pool_of,
    preflight_seed,
    snaps_match,
    swap_into_place,
    unmount_subtree,
)

if TYPE_CHECKING:
    from .migrate import Settings
    from .zfs import Zfs

TREE_SNAPSHOT = "enc-tree"
TREE_FINAL_SNAPSHOT = "enc-tree-final"


def _refuse_pool_root(dataset: str) -> None:
    if "/" not in dataset:
        msg = f"{dataset} is a pool root; the tree move is not supported"
        raise MigrationError(msg)


def _require_tree_snapshot(zfs: Zfs, name: str) -> None:
    if not zfs.exists(f"{name}@{TREE_SNAPSHOT}"):
        msg = f"{name}@{TREE_SNAPSHOT} does not exist; run tree-seed first"
        raise MigrationError(msg)


def tree_seed(zfs: Zfs, settings: Settings, dataset: str) -> list[str]:
    """Copy the parent alone to an encrypted staged dataset, leaving its key unloaded."""
    _refuse_pool_root(dataset)
    staged = f"{settings.stage}/{leaf_of(dataset)}"
    # The children move on their own, so only the parent's own data and snapshots are sized.
    preflight_seed(zfs, settings, dataset, "usedbydataset,usedbysnapshots")
    with abort_guard(zfs, settings, staged, label="tree-seed"):
        ensure_container(zfs, settings.stage)
        ensure_container(zfs, f"{settings.stage}/old")
        zfs.snapshot(f"{dataset}@{TREE_SNAPSHOT}", recursive=False)
        zfs.send_recv(["-p", f"{dataset}@{TREE_SNAPSHOT}"], encrypted_recv_args(settings, staged))
        zfs.unload_key(staged)
    return []


def tree_move(zfs: Zfs, settings: Settings, dataset: str, child: str) -> list[str]:
    """Copy one child subtree under the staged parent; the source stays until the operator destroys it."""
    leaf = leaf_of(dataset)
    staged_root = f"{settings.stage}/{leaf}"
    source = f"{dataset}/{child}"
    staged = f"{staged_root}/{child}"
    _require_tree_snapshot(zfs, staged_root)
    if not zfs.exists(source):
        msg = f"dataset {source} does not exist"
        raise MigrationError(msg)
    if zfs.exists(staged):
        msg = f"{staged} already exists"
        raise MigrationError(msg)
    check_free_space(zfs, settings, source, "used")
    load_stage_key(zfs, staged_root)
    with abort_guard(zfs, settings, staged_root, source=source, leaf=None, label="tree-move"):
        unmount_subtree(zfs, source)
        zfs.snapshot(f"{source}@{TREE_SNAPSHOT}", recursive=True)
        # A replication stream received under an encrypted parent stays unencrypted unless told to inherit.
        zfs.send_recv(["-R", f"{source}@{TREE_SNAPSHOT}"], ["-u", "-x", "encryption", staged])
        if not snaps_match(zfs, source, staged, TREE_SNAPSHOT):
            msg = f"{TREE_SNAPSHOT} snapshots of {source} and {staged} differ"
            raise MigrationError(msg)
        check_encrypted_under(zfs, staged, staged_root)
        zfs.pool_sync(pool_of(dataset))
        dirty = dirty_since(zfs, source, TREE_SNAPSHOT)
        if dirty:
            raise MigrationError(
                "written since the tree snapshot:\n" + "\n".join(dirty),
                [
                    "drop the stale snapshots and the partial copy, then retry tree-move:",
                    f"  zfs destroy -r {source}@{TREE_SNAPSHOT}",
                    f"  zfs destroy -r {staged}",
                ],
            )
        zfs.unload_key(staged_root)
    return [f"moved {source}; once verified, free it with: zfs destroy -r {source}"]


def _stale_final(zfs: Zfs, name: str, lines: list[str]) -> None:
    if zfs.exists(f"{name}@{TREE_FINAL_SNAPSHOT}"):
        lines.append(
            f"{name}@{TREE_FINAL_SNAPSHOT} already exists; drop it with: zfs destroy {name}@{TREE_FINAL_SNAPSHOT}",
        )


def tree_finish(zfs: Zfs, settings: Settings, dataset: str) -> list[str]:
    """Send the parent's last changes and swap the staged tree, with the moved children, into place."""
    _refuse_pool_root(dataset)
    leaf = leaf_of(dataset)
    staged = f"{settings.stage}/{leaf}"
    if pool_of(dataset) != pool_of(settings.stage):
        msg = (
            f"{dataset} is on pool {pool_of(dataset)} but the stage is on {pool_of(settings.stage)}; "
            "tree-finish cannot rename across pools"
        )
        raise MigrationError(msg)
    _require_tree_snapshot(zfs, staged)
    _require_tree_snapshot(zfs, dataset)
    if zfs.children(dataset):
        msg = f"children remain under {dataset}; move each with tree-move and destroy it first"
        raise MigrationError(msg)
    stale: list[str] = []
    _stale_final(zfs, dataset, stale)
    _stale_final(zfs, staged, stale)
    if stale:
        raise MigrationError("\n".join(stale))
    load_stage_key(zfs, staged)
    with abort_guard(zfs, settings, staged, source=dataset, leaf=leaf, label="tree-finish") as guard:
        unmount_subtree(zfs, dataset)
        zfs.snapshot(f"{dataset}@{TREE_FINAL_SNAPSHOT}", recursive=False)
        zfs.send_recv(["-p", "-I", f"@{TREE_SNAPSHOT}", f"{dataset}@{TREE_FINAL_SNAPSHOT}"], ["-u", staged])
        if not snaps_match(zfs, dataset, staged, TREE_FINAL_SNAPSHOT):
            msg = f"{TREE_FINAL_SNAPSHOT} snapshots of {dataset} and {staged} differ"
            raise MigrationError(msg)
        zfs.pool_sync(pool_of(dataset))
        dirty = dirty_since(zfs, dataset, TREE_FINAL_SNAPSHOT)
        if dirty:
            raise MigrationError(
                "written since the final snapshot:\n" + "\n".join(dirty),
                [
                    "drop the stale snapshots, then retry tree-finish:",
                    f"  zfs destroy {dataset}@{TREE_FINAL_SNAPSHOT}",
                    f"  zfs destroy {staged}@{TREE_FINAL_SNAPSHOT}",
                ],
            )
        swap_into_place(zfs, settings, dataset, staged, guard)
    return [f"tree-finish done; once verified, free the old copy with: zfs destroy -r {settings.stage}/old/{leaf}"]
