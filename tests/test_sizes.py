"""Tests for size parsing."""

from __future__ import annotations

import pytest

from zfs_encrypt_migrate.sizes import parse_size


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1T", 1099511627776),
        ("1048576", 1048576),
        ("512M", 512 * 1024**2),
        ("2GiB", 2 * 1024**3),
        ("4k", 4096),
        ("1pb", 1024**5),
        ("0", 0),
    ],
)
def test_parse_size_accepts_bytes_and_binary_suffixes(text: str, expected: int) -> None:
    """Plain bytes and K/M/G/T/P suffixes parse as binary multiples."""
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["1X", "", "-1", "1.5G", "G", "1 G"])
def test_parse_size_rejects_invalid_input(text: str) -> None:
    """Anything but digits plus an optional binary suffix is refused."""
    with pytest.raises(ValueError, match="invalid size"):
        parse_size(text)
