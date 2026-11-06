"""Tests for size parsing."""

from __future__ import annotations

import pytest

from zfs_encrypt_migrate.sizes import format_duration, format_size, parse_size


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


@pytest.mark.parametrize(
    ("size", "expected"),
    [(105_381_888_000, "98.1G"), (512, "512B"), (1536, "1.5K")],
)
def test_format_size(size: int, expected: str) -> None:
    """Sizes print in binary units with one decimal."""
    assert format_size(size) == expected


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (59, "59s"),
        (60, "1m 0s"),
        (3490, "58m 10s"),
        (3600, "1h 0m"),
        (3601, "1h 0m"),
        (3900, "1h 5m"),
        (86_401, "1d 0h"),
        (183_600, "2d 3h"),
    ],
)
def test_format_duration(seconds: int, text: str) -> None:
    """A duration shows two units, from its largest non-zero one."""
    assert format_duration(seconds) == text
