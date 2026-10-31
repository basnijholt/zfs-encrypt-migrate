"""Tests for property planning."""

from __future__ import annotations

from zfs_encrypt_migrate.props import create_props, fixups
from zfs_encrypt_migrate.zfs import Prop


def local(value: str) -> Prop:
    """Build a locally set property."""
    return Prop(value, "local")


def test_create_props_copies_local_and_received_except_deferred() -> None:
    """Create props copies local and received except deferred."""
    source = {
        "compression": local("zstd"),
        "mountpoint": local("/srv/m"),
        "sharenfs": local("on"),
        "com.example:x": local("1"),
        "atime": Prop("off", "received"),
        "quota": Prop("none", "default"),
    }
    result = create_props(source)
    assert result["compression"] == "zstd"
    assert result["com.example:x"] == "1"
    assert result["atime"] == "off"
    assert "mountpoint" not in result
    assert "sharenfs" not in result
    assert "quota" not in result


def test_create_props_pins_inherited_write_affecting_values() -> None:
    """Create props pins inherited write affecting values."""
    source = {"recordsize": Prop("1048576", "inherited from tank/a")}
    assert create_props(source) == {"recordsize": "1048576"}


def test_create_props_passes_create_only_values() -> None:
    """Create props passes create only values."""
    source = {"casesensitivity": Prop("sensitive", "-")}
    assert create_props(source) == {"casesensitivity": "sensitive"}


def test_create_props_skips_markers_and_encryption() -> None:
    """Create props skips markers and encryption."""
    source = {
        "zfs-encrypt-migrate:file-move": local("x"),
        "encryption": local("aes-256-gcm"),
        "keyformat": local("raw"),
        "keylocation": local("file:///k"),
        "pbkdf2iters": local("1"),
        "reservation": local("1G"),
        "canmount": local("noauto"),
    }
    assert create_props(source) == {}


def test_fixups_local_mountpoint_and_shares() -> None:
    """Fixups local mountpoint and shares."""
    source = {
        "mountpoint": local("/srv/m"),
        "sharenfs": local("on"),
        "sharesmb": Prop("on", "received"),
        "reservation": local("1G"),
        "refreservation": local("2G"),
    }
    result = fixups(source)
    assert result.set == {
        "mountpoint": "/srv/m",
        "sharenfs": "on",
        "sharesmb": "on",
        "reservation": "1G",
        "refreservation": "2G",
    }
    assert result.inherit == ()


def test_fixups_inherits_mountpoint_and_write_affecting() -> None:
    """Fixups inherits mountpoint and write affecting."""
    source = {
        "mountpoint": Prop("/tank/a", "inherited from tank"),
        "compression": Prop("lz4", "default"),
        "recordsize": local("1M"),
    }
    result = fixups(source)
    assert result.inherit == ("mountpoint", "compression")
    assert result.set == {}


def test_fixups_canmount_default_on() -> None:
    """Fixups canmount default on."""
    assert fixups({}).canmount == "on"


def test_fixups_canmount_local_noauto() -> None:
    """Fixups canmount local noauto."""
    assert fixups({"canmount": local("noauto")}).canmount == "noauto"
