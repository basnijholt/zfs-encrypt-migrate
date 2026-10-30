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
    outside_encryption_root,
    pool_of,
    preflight_seed,
    refuse_cross_pool,
    refuse_nested,
    refuse_stale,
    rename_aside_commands,
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


def _start_over(source: str, staged: str) -> list[str]:
    """Commands that remove what an unfinished tree-move left behind, so the next one can start over."""
    return [f"  zfs destroy -r {source}@{TREE_SNAPSHOT}", f"  zfs destroy -r {staged}"]


def _moved(source: str, staged_root: str) -> list[str]:
    # Destroying the source leaves the staged copy as the only one while its key still comes from the
    # key file, so the check asks for the passphrase as stored elsewhere instead of reading that file.
    return [
        f"verify the key with: zfs load-key -n -L prompt {staged_root}",
        f"moved {source}; once verified, free it with: zfs destroy -r {source}",
    ]


def _earlier_move(zfs: Zfs, source: str, staged: str, staged_root: str) -> list[str]:
    """Report a finished earlier move again; refuse the partial copy an interrupted or failed one left."""
    # An interrupted receive leaves the child with descendants or its newest snapshots missing.
    if snaps_match(zfs, source, staged, TREE_SNAPSHOT) and outside_encryption_root(zfs, staged, staged_root) is None:
        return [f"{source} was already moved to {staged}", *_moved(source, staged_root)]
    msg = f"{staged} already exists but is not a complete copy of {source}; a tree-move was interrupted or failed"
    raise MigrationError(
        msg,
        ["remove the partial copy and the tree snapshots, then rerun tree-move:", *_start_over(source, staged)],
    )


def tree_seed(zfs: Zfs, settings: Settings, dataset: str) -> list[str]:
    """Copy the parent alone to an encrypted staged dataset, leaving its key unloaded."""
    _refuse_pool_root(dataset)
    refuse_cross_pool(settings, dataset, "tree-seed")
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
    refuse_cross_pool(settings, dataset, "tree-move")
    refuse_nested(settings, dataset)
    staged_root = f"{settings.stage}/{leaf_of(dataset)}"
    source = f"{dataset}/{child}"
    staged = f"{staged_root}/{child}"
    _require_tree_snapshot(zfs, staged_root)
    if not zfs.exists(source):
        msg = f"dataset {source} does not exist"
        raise MigrationError(msg)
    if zfs.exists(staged):
        return _earlier_move(zfs, source, staged, staged_root)
    check_free_space(zfs, settings, source, "used")
    # Armed before the load so an interrupt while the key loads still unloads it; the remount hint only
    # applies once the source is about to be unmounted.
    with abort_guard(zfs, settings, staged_root, label="tree-move") as guard:
        load_stage_key(zfs, staged_root)
        guard.source = source
        unmount_subtree(zfs, source)
        zfs.snapshot(f"{source}@{TREE_SNAPSHOT}", recursive=True)
        # From here on a rerun refuses until the tree snapshots and any partial copy are gone.
        guard.cleanup = [
            f"{staged} may hold a partial copy; remove it and the tree snapshots, then rerun tree-move:",
            *_start_over(source, staged),
        ]
        # A replication stream received under an encrypted parent stays unencrypted unless told to inherit.
        zfs.send_recv(["-R", f"{source}@{TREE_SNAPSHOT}"], ["-u", "-x", "encryption", staged])
        if not snaps_match(zfs, source, staged, TREE_SNAPSHOT):
            msg = f"{TREE_SNAPSHOT} snapshots of {source} and {staged} differ"
            raise MigrationError(msg)
        check_encrypted_under(zfs, staged, staged_root)
        zfs.pool_sync(pool_of(dataset))
        dirty = dirty_since(zfs, source, TREE_SNAPSHOT)
        if dirty:
            # The guard's cleanup hint says how to start over.
            raise MigrationError("written since the tree snapshot:\n" + "\n".join(dirty))
        zfs.unload_key(staged_root)
    return _moved(source, staged_root)


def tree_finish(zfs: Zfs, settings: Settings, dataset: str) -> list[str]:
    """Send the parent's last changes and swap the staged tree, with the moved children, into place."""
    _refuse_pool_root(dataset)
    leaf = leaf_of(dataset)
    staged = f"{settings.stage}/{leaf}"
    refuse_cross_pool(settings, dataset, "tree-finish")
    refuse_nested(settings, dataset)
    _require_tree_snapshot(zfs, staged)
    _require_tree_snapshot(zfs, dataset)
    if zfs.children(dataset):
        msg = f"children remain under {dataset}; move each with tree-move and destroy it first"
        raise MigrationError(msg)
    # The parent's final snapshot is taken without -r, so it is renamed aside without -r too.
    refuse_stale(zfs, TREE_FINAL_SNAPSHOT, [dataset, staged], recursive=False)
    with abort_guard(zfs, settings, staged, label="tree-finish") as guard:
        load_stage_key(zfs, staged)
        guard.source = dataset
        guard.leaf = leaf
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
                    "rename the stale snapshots aside, then retry tree-finish:",
                    *(
                        f"  {command}"
                        for command in rename_aside_commands(
                            zfs,
                            TREE_FINAL_SNAPSHOT,
                            [dataset, staged],
                            recursive=False,
                        )
                    ),
                ],
            )
        swap_into_place(zfs, settings, dataset, staged, guard)
    return [
        f"verify the key with: zfs load-key -n {dataset}",
        f"tree-finish done; once verified, free the old copy with: zfs destroy -r {settings.stage}/old/{leaf}",
    ]
