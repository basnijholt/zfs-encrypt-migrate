"""Tests for the CLI entry point."""

from __future__ import annotations

from typer.testing import CliRunner

from zfs_encrypt_migrate import __version__
from zfs_encrypt_migrate.cli import app
from zfs_encrypt_migrate.errors import MigrationError, ZfsError
from zfs_encrypt_migrate.process import CommandResult


def test_version_flag_prints_version() -> None:
    """--version and -v print the package version."""
    runner = CliRunner()
    for flag in ("--version", "-v"):
        result = runner.invoke(app, [flag])
        assert result.exit_code == 0
        assert result.output.strip() == f"zfs-encrypt-migrate {__version__}"


def test_zfs_error_carries_command_and_stderr() -> None:
    """ZfsError names the failing command and its stderr."""
    failed = CommandResult(returncode=1, stdout="", stderr="cannot open 'x': dataset does not exist\n")

    error = ZfsError(failed, ["zfs", "list", "x"])

    assert isinstance(error, MigrationError)
    assert "zfs list x" in str(error)
    assert "dataset does not exist" in str(error)
    assert error.hints == ()
