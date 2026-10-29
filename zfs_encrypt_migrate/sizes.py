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
