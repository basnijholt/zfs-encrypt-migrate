"""Size parsing."""

from __future__ import annotations

import re

_SIZE_RE = re.compile(r"^(?P<number>\d+)(?:(?P<unit>[KMGTP])(?:i?B)?|B)?$", re.IGNORECASE)
_UNITS = {unit: 1024 ** (power + 1) for power, unit in enumerate("KMGTP")}


def parse_size(text: str) -> int:
    """Parse plain bytes or a binary K/M/G/T/P size such as ``1T`` or ``2GiB``."""
    match = _SIZE_RE.fullmatch(text)
    if match is None:
        msg = f"invalid size {text!r}: use bytes or a K/M/G/T/P suffix such as 512M or 1T"
        raise ValueError(msg)
    unit = match["unit"]
    return int(match["number"]) * (_UNITS[unit.upper()] if unit else 1)


def format_size(size: int) -> str:
    """Format bytes in binary units: whole bytes, else one decimal and a K/M/G/T/P suffix."""
    if size < 1024:  # noqa: PLR2004
        return f"{size}B"
    value = float(size)
    for unit in "KMGTP":
        value /= 1024
        if value < 1024 or unit == "P":  # noqa: PLR2004
            return f"{value:.1f}{unit}"
    raise AssertionError  # pragma: no cover


def format_duration(seconds: float) -> str:
    """Format a duration with its two most significant non-zero units, e.g. ``2d 3h``, ``58m 10s`` or ``45s``."""
    rest = int(max(seconds, 0))
    parts = []
    for unit, length in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, rest = divmod(rest, length)
        if count:
            parts.append(f"{count}{unit}")
    return " ".join(parts[:2]) or "0s"
