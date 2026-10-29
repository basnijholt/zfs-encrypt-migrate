"""Audit pools for datasets that are not natively encrypted."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .zfs import Zfs


def _covered(name: str, exact: set[str], subtrees: set[str]) -> bool:
    if name in exact:
        return True
    # Walk up by path component so tank/six covers tank/six/a but not tank/sixty.
    parts = name.split("/")
    return any("/".join(parts[:i]) in subtrees for i in range(1, len(parts) + 1))


def find_unencrypted(zfs: Zfs, pools: Sequence[str], exact: Sequence[str], subtrees: Sequence[str]) -> list[str]:
    """Return unencrypted filesystems and volumes outside the allowlists.

    One listing covers every pool, and any zfs error propagates: a pool that is
    not imported must never read as "nothing unencrypted".
    """
    listing = zfs.list_encryption(pools)
    exact_set, subtree_set = set(exact), set(subtrees)
    return [name for name, encryption in listing if encryption == "off" and not _covered(name, exact_set, subtree_set)]
