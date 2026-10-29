"""Tests for the audit CLI command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from zfs_encrypt_migrate import cli
from zfs_encrypt_migrate.constants import COMMAND_STARTUP_ERROR_RETURNCODE

from .conftest import FakeRunner, fail, ok
from .zfs_sim import ZfsSim

if TYPE_CHECKING:
    from pathlib import Path

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


# --- migration commands ---


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ZfsSim:
    """Install a simulated pool behind the CLI with a private key file."""
    simulator = ZfsSim(["tank", "tank/data"])
    simulator.roots = ["tank/data", "tank/stage/data"]
    fake_runner = FakeRunner(simulator.answer)
    monkeypatch.setattr(cli, "_runner", lambda: fake_runner)
    key = tmp_path / "key"
    key.write_text("secret")
    key.chmod(0o600)
    monkeypatch.setenv("ZFS_ENCRYPT_MIGRATE_KEY_FILE", str(key))
    monkeypatch.setenv("ZFS_ENCRYPT_MIGRATE_MIN_FREE", "1000")
    monkeypatch.setenv("ZFS_ENCRYPT_MIGRATE_STAGE", "tank/stage")
    simulator.runner = fake_runner  # type: ignore[attr-defined]
    return simulator


def test_seed_runs_through_options_and_env(sim: ZfsSim) -> None:
    """Env vars supply the shared options; success exits 0."""
    result = runner.invoke(cli.app, ["seed", "tank/data"])

    assert result.exit_code == 0, result.output
    assert sim.runner.pipelines[0][0] == ("zfs", "send", "-R", "tank/data@enc-seed")  # type: ignore[attr-defined]


def test_options_override_env(sim: ZfsSim) -> None:
    """Flags beat env vars; keyformat and size are applied."""
    result = runner.invoke(
        cli.app,
        ["seed", "tank/data", "--stage", "tank/other", "--keyformat", "hex", "--min-free", "1K"],
    )

    assert result.exit_code == 0, result.output
    recv = sim.runner.pipelines[0][1]  # type: ignore[attr-defined]
    assert "keyformat=hex" in recv
    assert recv[-1] == "tank/other/data"


@pytest.mark.parametrize(
    "argv",
    [
        ["seed", "tank/data", "--keyformat", "bogus"],
        ["seed", "tank/data", "--min-free", "lots"],
        ["seed", "tank/data", "--stage", "-x"],
        ["seed", "-r"],
        ["seed", "tank/data@s"],
        ["catchup", "tank/a b"],
        ["cutover", "--", "-r"],
    ],
)
def test_migration_commands_reject_bad_input_before_zfs(sim: ZfsSim, argv: list[str]) -> None:
    """Bad option values and names exit 2 and never reach zfs."""
    result = runner.invoke(cli.app, argv)

    assert result.exit_code == 2  # noqa: PLR2004
    assert sim.runner.calls == []  # type: ignore[attr-defined]


def test_stage_is_required(sim: ZfsSim, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --stage or its env var the command is a usage error."""
    monkeypatch.delenv("ZFS_ENCRYPT_MIGRATE_STAGE")

    result = runner.invoke(cli.app, ["seed", "tank/data"])

    assert result.exit_code == 2  # noqa: PLR2004
    assert sim.runner.calls == []  # type: ignore[attr-defined]


def test_migration_refusal_prints_error_and_hints_exit_1(sim: ZfsSim) -> None:
    """A refusal prints one error plus the hints and exits 1 without a traceback."""
    sim.datasets |= {"tank/stage", "tank/stage/old", "tank/stage/data", "tank/stage/data/c"}
    sim.props["tank/stage/data", "keystatus"] = "unavailable"

    result = runner.invoke(cli.app, ["cutover", "tank/data"])

    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output


@pytest.mark.usefixtures("sim")
def test_cutover_pool_root_exit_1() -> None:
    """Cutover of a pool root prints the refusal."""
    result = runner.invoke(cli.app, ["cutover", "tank"])

    assert result.exit_code == 1
    assert "tank is a pool root; cutover is not supported" in result.output


@pytest.mark.parametrize("command", ["seed", "catchup", "cutover"])
def test_missing_zfs_binary_is_one_error_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str) -> None:
    """A zfs binary that cannot start is one clean error, never a traceback."""
    key = tmp_path / "key"
    key.write_text("secret")
    key.chmod(0o600)
    fake_runner = FakeRunner(lambda _args: fail("No such file or directory: 'zfs'\n", COMMAND_STARTUP_ERROR_RETURNCODE))
    monkeypatch.setattr(cli, "_runner", lambda: fake_runner)

    result = runner.invoke(cli.app, [command, "tank/data", "--stage", "tank/stage", "--key-file", str(key)])

    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_keyboard_interrupt_prints_interrupted_hints_and_exits_130(
    sim: ZfsSim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C mid-pipeline unloads the key, prints the hints and exits 130."""
    sim.datasets |= {"tank/stage", "tank/stage/old", "tank/stage/data"}
    sim.props["tank/stage/data", "keystatus"] = "available"
    for name in ("tank/data", "tank/stage/data"):
        sim.snapshot(f"{name}@enc-seed")
        sim.props[name, "mounted"] = "yes"

    def interrupt(_first: object, _second: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(sim.runner, "pipeline", interrupt)  # type: ignore[attr-defined]

    result = runner.invoke(cli.app, ["cutover", "tank/data"])

    assert result.exit_code == 130  # noqa: PLR2004
    assert "interrupted" in result.output
    assert "cutover aborted." in result.output
    assert ("zfs", "unload-key", "-r", "tank/stage/data") in sim.runner.calls  # type: ignore[attr-defined]


def test_migration_commands_are_in_their_own_help_panel() -> None:
    """The three commands are listed under Migration Commands."""
    result = runner.invoke(cli.app, ["--help"])

    assert "Migration Commands" in result.output
    for name in ("seed", "catchup", "cutover"):
        assert name in result.output
