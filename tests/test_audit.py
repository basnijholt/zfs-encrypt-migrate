"""Tests for the encryption audit."""

from __future__ import annotations

import pytest

from zfs_encrypt_migrate.audit import find_unencrypted
from zfs_encrypt_migrate.errors import ZfsError
from zfs_encrypt_migrate.zfs import Zfs

from .conftest import FakeRunner, fail, ok

LISTING = (
    "tank\toff\ntank/share\taes-256-gcm\ntank/plain\toff\ntank/plain/sub\toff\n"
    "tank/six\toff\ntank/six/a\toff\ntank/sixty\toff\ntank/stage\toff\n"
    "tank/stage/old\toff\ntank/stage/old/share\toff\nssd\toff\n"
)


def _audit(exact: list[str], subtrees: list[str], pools: list[str] | None = None) -> tuple[list[str], FakeRunner]:
    runner = FakeRunner(queue=[ok(LISTING)])
    return find_unencrypted(Zfs(runner), pools or ["tank", "ssd"], exact, subtrees), runner


def test_one_listing_call_for_all_pools() -> None:
    """All pools go in a single zfs list."""
    _, runner = _audit([], [])

    assert runner.calls == [
        ("zfs", "list", "-rH", "-t", "filesystem,volume", "-o", "name,encryption", "tank", "ssd"),
    ]


def test_pool_roots_are_listed_unless_allowed() -> None:
    """Pool roots are ordinary rows."""
    bad, _ = _audit(["tank/stage/old"], ["tank/plain", "tank/six", "tank/sixty", "tank/stage/old/share"])

    assert bad == ["tank", "tank/stage", "ssd"]


def test_exact_matches_whole_name_only() -> None:
    """--exact does not cover descendants."""
    bad, _ = _audit(["tank", "ssd", "tank/stage", "tank/stage/old"], [])

    assert "tank/stage/old/share" in bad
    assert "tank" not in bad
    assert "tank/share" not in bad  # encrypted


def test_subtree_matches_by_path_component() -> None:
    """tank/six covers tank/six/a but not tank/sixty."""
    bad, _ = _audit(["tank", "ssd"], ["tank/six", "tank/plain", "tank/stage"])

    assert bad == ["tank/sixty"]


def test_clean_pools_return_empty() -> None:
    """Nothing outside the allowlist means no offenders."""
    bad, _ = _audit(["tank", "ssd"], ["tank/six", "tank/sixty", "tank/plain", "tank/stage"])

    assert bad == []


def test_zfs_failure_fails_closed() -> None:
    """A pool that is not imported raises an error naming it."""
    runner = FakeRunner(queue=[fail("cannot open 'nopool': dataset does not exist\n")])

    with pytest.raises(ZfsError, match="nopool"):
        find_unencrypted(Zfs(runner), ["tank", "nopool"], [], [])
