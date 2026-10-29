"""Tests for the audit CLI command."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from zfs_encrypt_migrate import cli
from zfs_encrypt_migrate.constants import COMMAND_STARTUP_ERROR_RETURNCODE

from .conftest import FakeRunner, fail, ok

runner = CliRunner()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Install a fake runner behind the CLI."""
    fake_runner = FakeRunner()
    monkeypatch.setattr(cli, "_runner", lambda: fake_runner)
    return fake_runner


def test_audit_prints_offenders_and_exits_1(fake: FakeRunner) -> None:
    """Offenders go to stdout one per line, exit 1."""
    fake.queue.append(ok("tank\toff\ntank/a\toff\ntank/b\taes-256-gcm\n"))

    result = runner.invoke(cli.app, ["audit", "--exact", "tank", "tank"])

    assert result.exit_code == 1
    assert result.stdout == "tank/a\n"


def test_audit_clean_exits_0(fake: FakeRunner) -> None:
    """No offenders print nothing and exit 0."""
    fake.queue.append(ok("tank\taes-256-gcm\n"))

    result = runner.invoke(cli.app, ["audit", "tank"])

    assert result.exit_code == 0
    assert result.stdout == ""


def test_audit_without_pools_is_usage_error(fake: FakeRunner) -> None:
    """No pools exits 2 without calling zfs."""
    result = runner.invoke(cli.app, ["audit", "--exact", "tank"])

    assert result.exit_code == 2  # noqa: PLR2004
    assert fake.calls == []


def test_audit_zfs_failure_names_pool(fake: FakeRunner) -> None:
    """A missing pool prints one error naming it and exits 1."""
    fake.queue.append(fail("cannot open 'nopool': dataset does not exist\n"))

    result = runner.invoke(cli.app, ["audit", "nopool"])

    assert result.exit_code == 1
    assert "nopool" in result.output
    assert "Traceback" not in result.output


def test_audit_missing_zfs_binary_is_one_error_line(fake: FakeRunner) -> None:
    """A zfs binary that cannot start exits 1 with a clean message."""
    fake.queue.append(fail("No such file or directory: 'zfs'\n", COMMAND_STARTUP_ERROR_RETURNCODE))

    result = runner.invoke(cli.app, ["audit", "tank"])

    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["audit", "-r"],
        ["audit", "--", "-r"],
        ["audit", "tank@snap"],
        ["audit", "tank a"],
        ["audit", "--exact", "-x", "tank"],
        ["audit", "--subtree", "tank/a@s", "tank"],
    ],
)
def test_audit_rejects_bad_names_before_zfs(fake: FakeRunner, argv: list[str]) -> None:
    """Option-like, snapshot and spaced names never reach zfs."""
    result = runner.invoke(cli.app, argv)

    assert result.exit_code == 2  # noqa: PLR2004
    assert fake.calls == []
