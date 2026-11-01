"""Plan which ZFS properties a staged dataset gets and what is fixed before the swap."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .zfs import Prop

# Applied only at the swap: they would mount, share or reserve space on the staged copy.
_DEFERRED = ("mountpoint", "canmount", "sharenfs", "sharesmb", "reservation", "refreservation")
# Pinned at creation to the source's value so data is written with the same layout;
# fixups() re-inherits those that were inherited so the final sources match.
_WRITE_AFFECTING = (
    "recordsize",
    "compression",
    "checksum",
    "copies",
    "dedup",
    "xattr",
    "dnodesize",
    "acltype",
    "special_small_blocks",
    "redundant_metadata",
)
# Cannot be changed after creation, so always passed explicitly.
_CREATE_ONLY = ("casesensitivity", "normalization", "utf8only")

_MARKER_PREFIX = "zfs-encrypt-migrate:"
_ENCRYPTION = ("encryption", "keyformat", "keylocation", "pbkdf2iters")
_SET_AT_SWAP = ("mountpoint", "sharenfs", "sharesmb", "reservation", "refreservation")


def _explicit(prop: Prop) -> bool:
    return prop.source in ("local", "received")


def create_props(source: Mapping[str, Prop]) -> dict[str, str]:
    """Return the properties to pass to ``zfs create`` for the staged dataset."""
    result = {
        name: prop.value
        for name, prop in source.items()
        if _explicit(prop) and name not in _DEFERRED and name not in _ENCRYPTION and not name.startswith(_MARKER_PREFIX)
    }
    for name in (*_WRITE_AFFECTING, *_CREATE_ONLY):
        if name in source:
            result[name] = source[name].value
    return result


@dataclass(frozen=True)
class _Fixups:
    """Property changes applied to the staged dataset just before the swap."""

    set: dict[str, str]
    inherit: tuple[str, ...]
    canmount: str


def fixups(source: Mapping[str, Prop]) -> _Fixups:
    """Return the fix-ups that make the swapped dataset match the source's properties."""
    to_set = {name: source[name].value for name in _SET_AT_SWAP if name in source and _explicit(source[name])}
    inherit = []
    if "mountpoint" in source and not _explicit(source["mountpoint"]):
        inherit.append("mountpoint")
    inherit.extend(name for name in _WRITE_AFFECTING if name in source and not _explicit(source[name]))
    canmount = source.get("canmount")
    return _Fixups(
        set=to_set,
        inherit=tuple(inherit),
        canmount=canmount.value if canmount is not None and _explicit(canmount) else "on",
    )
