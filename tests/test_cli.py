"""Tests for the CLI commands."""

from __future__ import annotations

import os
import signal
import threading
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from zfs_encrypt_migrate import cli
from zfs_encrypt_migrate.constants import COMMAND_STARTUP_ERROR_RETURNCODE
from zfs_encrypt_migrate.filemove import probe_writers
from zfs_encrypt_migrate.process import SubprocessRunner

from .conftest import FakeRunner, fail, ok
from .zfs_sim import ZfsSim, staged_ready

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from zfs_encrypt_migrate.process import CommandResult
    from zfs_encrypt_migrate.zfs import Zfs

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
        ["seed", "tank/data", "--final-keylocation", "promt"],  # codespell:ignore
        ["seed", "tank/data", "--final-keylocation", "file://relative"],
        ["seed", "tank/data", "--final-keylocation", "file:///"],
        ["seed", "tank/data", "--final-keylocation", "https://"],
        ["seed", "tank/data", "--key-file", "key"],
        ["catchup", "tank/data", "--key-file", "./run/key"],
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


def test_relative_key_file_from_env_is_usage_error(sim: ZfsSim, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative key file from the env var is refused too: zfs would only reject it after the seed snapshots."""
    monkeypatch.setenv("ZFS_ENCRYPT_MIGRATE_KEY_FILE", "zfs-encrypt-migrate.key")

    result = runner.invoke(cli.app, ["seed", "tank/data"])

    assert result.exit_code == 2  # noqa: PLR2004
    assert "must be an absolute path" in result.output
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


@pytest.mark.parametrize("value", ["prompt", "file:///etc/zfs/key", "https://keys.example/key", "http://k/x"])
@pytest.mark.usefixtures("sim")
def test_final_keylocation_accepts_zfs_forms(value: str) -> None:
    """Prompt, absolute file URLs and http(s) URLs pass validation."""
    result = runner.invoke(cli.app, ["seed", "tank/data", "--final-keylocation", value])

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_real_signal_mid_pipeline_cleans_up_and_exits_130(
    sim: ZfsSim,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signum: signal.Signals,
) -> None:
    """SIGTERM and SIGHUP take the Ctrl-C path: children stopped, key unloaded, hints, exit 130."""
    staged_ready(sim)
    sim.props["tank/stage/data", "keystatus"] = "available"
    sim.props["tank/data", "mounted"] = "yes"
    pid_files = (tmp_path / "first.pid", tmp_path / "second.pid")

    class Blocking(FakeRunner):
        def pipeline(self, first: Sequence[str], second: Sequence[str]) -> tuple[CommandResult, CommandResult]:
            self.pipelines.append((tuple(first), tuple(second)))
            return SubprocessRunner().pipeline(
                ["sh", "-c", f"echo $$ > {pid_files[0]}; exec sleep 60"],
                ["sh", "-c", f"echo $$ > {pid_files[1]}; exec sleep 60"],
            )

    blocking = Blocking(sim.answer)
    monkeypatch.setattr(cli, "_runner", lambda: blocking)
    before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    stop = threading.Event()

    def signal_once_children_run() -> None:
        # Fire only when both children wrote their pid, so slow machines cannot signal too early.
        while not stop.wait(0.05):
            if all(f.exists() and f.read_text().strip() for f in pid_files):
                os.kill(os.getpid(), signum)
                return

    thread = threading.Thread(target=signal_once_children_run)
    thread.start()
    try:
        result = runner.invoke(cli.app, ["cutover", "tank/data"])
    finally:
        stop.set()
        thread.join()

    assert result.exit_code == 130  # noqa: PLR2004
    assert "interrupted" in result.output
    assert "cutover aborted." in result.output
    assert sim.props["tank/stage/data", "keystatus"] == "unavailable"
    for pid_file in pid_files:
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    assert {number: signal.getsignal(number) for number in before} == before


def test_signal_ignored_on_entry_stays_ignored(sim: ZfsSim, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under nohup SIGHUP starts ignored; a hangup mid-run must not abort the migration."""
    seen: list[object] = []

    def hang_up(args: Sequence[str]) -> CommandResult:
        if args[1] == "snapshot":
            seen.append(signal.getsignal(signal.SIGHUP))
            signal.raise_signal(signal.SIGHUP)
        return sim.answer(args)

    monkeypatch.setattr(cli, "_runner", lambda: FakeRunner(hang_up))
    sigterm = signal.getsignal(signal.SIGTERM)
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        result = runner.invoke(cli.app, ["seed", "tank/data"])
        after = signal.getsignal(signal.SIGHUP)
    finally:
        signal.signal(signal.SIGHUP, previous)

    assert result.exit_code == 0, result.output
    assert seen == [signal.SIG_IGN]
    assert after is signal.SIG_IGN
    assert signal.getsignal(signal.SIGTERM) is sigterm


# --- tree commands ---


def _tree_state(sim: ZfsSim) -> None:
    """Stage the parent of tank/data as tree-seed leaves it, with one child to move."""
    sim.datasets |= {"tank/stage", "tank/stage/old", "tank/stage/data", "tank/data/child"}
    sim.snapshot("tank/data@enc-tree")
    sim.snapshot("tank/stage/data@enc-tree")
    sim.prop("tank/stage/data", "encryption", "aes-256-gcm")
    sim.prop("tank/stage/data", "encryptionroot", "tank/stage/data")
    sim.prop("tank/stage/data", "keystatus", "unavailable")


def test_tree_seed_runs_through_options_and_env(sim: ZfsSim) -> None:
    """tree-seed uses the shared options and exits 0."""
    result = runner.invoke(cli.app, ["tree-seed", "tank/data"])

    assert result.exit_code == 0, result.output
    assert sim.runner.pipelines[0][0] == ("zfs", "send", "-p", "tank/data@enc-tree")  # type: ignore[attr-defined]


def test_tree_move_runs_and_prints_destroy_hint(sim: ZfsSim) -> None:
    """tree-move takes the parent and the child name and prints the key check, then the verified-destroy hint."""
    _tree_state(sim)

    result = runner.invoke(cli.app, ["tree-move", "tank/data", "child"])

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "verify the key with: zfs load-key -n -L prompt tank/stage/data\n"
        "moved tank/data/child; once verified, free it with: zfs destroy -r tank/data/child\n"
    )
    assert sim.runner.pipelines[0][0] == ("zfs", "send", "-R", "tank/data/child@enc-tree")  # type: ignore[attr-defined]


def test_tree_finish_runs(sim: ZfsSim) -> None:
    """tree-finish refuses while the child remains, with exit 1 and the refusal text."""
    _tree_state(sim)

    result = runner.invoke(cli.app, ["tree-finish", "tank/data"])

    assert result.exit_code == 1
    assert "children remain under tank/data; move each with tree-move and destroy it first" in result.output


@pytest.mark.parametrize("command", ["tree-seed", "tree-finish"])
@pytest.mark.usefixtures("sim")
def test_tree_commands_refuse_pool_root(command: str) -> None:
    """Pool roots exit 1 with the tree-move wording."""
    result = runner.invoke(cli.app, [command, "tank"])

    assert result.exit_code == 1
    assert "tank is a pool root; the tree move is not supported" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["tree-move", "tank/data", "a/b"],
        ["tree-move", "tank/data", "a@b"],
        ["tree-move", "tank/data", ""],
        ["tree-move", "tank/data", "--", "-x"],
        ["tree-move", "tank/data", "a b"],
        ["tree-move", "tank/data"],
        ["tree-move", "--", "-r", "child"],
        ["tree-seed", "tank/data@s"],
        ["tree-finish", "--", "-r"],
        ["tree-finish", "tank/data", "--min-free", "lots"],
    ],
)
def test_tree_commands_reject_bad_input_before_zfs(sim: ZfsSim, argv: list[str]) -> None:
    """Bad names, child paths and options exit 2 and never reach zfs."""
    result = runner.invoke(cli.app, argv)

    assert result.exit_code == 2  # noqa: PLR2004
    assert sim.runner.calls == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "argv",
    [["tree-seed", "tank/data"], ["tree-move", "tank/data", "child"], ["tree-finish", "tank/data"]],
)
def test_tree_commands_missing_zfs_binary_is_one_error_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
) -> None:
    """A zfs binary that cannot start is one clean error, never a traceback."""
    key = tmp_path / "key"
    key.write_text("secret")
    key.chmod(0o600)
    fake_runner = FakeRunner(lambda _args: fail("No such file or directory: 'zfs'\n", COMMAND_STARTUP_ERROR_RETURNCODE))
    monkeypatch.setattr(cli, "_runner", lambda: fake_runner)

    result = runner.invoke(cli.app, [*argv, "--stage", "tank/stage", "--key-file", str(key)])

    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_tree_move_interrupt_prints_tree_move_hints_and_exits_130(
    sim: ZfsSim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C mid-pipeline unloads the key, prints the tree-move hints and exits 130."""
    _tree_state(sim)
    sim.props["tank/stage/data", "keystatus"] = "available"

    def interrupt(_first: object, _second: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(sim.runner, "pipeline", interrupt)  # type: ignore[attr-defined]

    result = runner.invoke(cli.app, ["tree-move", "tank/data", "child"])

    assert result.exit_code == 130  # noqa: PLR2004
    assert "tree-move aborted." in result.output
    assert "tank/stage/data/child may hold a partial copy" in result.output
    assert "  zfs destroy -r tank/data/child@enc-tree\n  zfs destroy -r tank/stage/data/child\n" in result.output
    assert ("zfs", "unload-key", "-r", "tank/stage/data") in sim.runner.calls  # type: ignore[attr-defined]


def test_tree_move_partial_copy_exits_1_with_cleanup(sim: ZfsSim) -> None:
    """A copy left by an interrupted run is refused with the commands that remove it."""
    _tree_state(sim)
    sim.snapshot("tank/data/child@enc-tree")
    sim.datasets.add("tank/stage/data/child")

    result = runner.invoke(cli.app, ["tree-move", "tank/data", "child"])

    assert result.exit_code == 1
    assert "not a complete copy of tank/data/child" in result.output
    assert "  zfs destroy -r tank/data/child@enc-tree\n  zfs destroy -r tank/stage/data/child\n" in result.output
    assert "moved" not in result.output


def test_tree_move_help_names_the_parent() -> None:
    """DATASET of tree-move is the parent, not the dataset being moved."""
    result = runner.invoke(cli.app, ["tree-move", "--help"])

    assert "Parent dataset, as given to tree-seed" in result.output


def test_tree_commands_are_in_their_own_help_panel() -> None:
    """The three commands are listed under Tree Commands."""
    result = runner.invoke(cli.app, ["--help"])

    assert "Tree Commands" in result.output
    for name in ("tree-seed", "tree-move", "tree-finish"):
        assert name in result.output


# --- file-move ---


def test_file_move_help_lists_options() -> None:
    """The help names the batch size, its env var and the delete confirmation."""
    result = runner.invoke(cli.app, ["file-move", "--help"], env={"COLUMNS": "200"})
    listing = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    for text in ("--batch-size", "--delete-source", "Target size of each batch"):
        assert text in result.output
    assert "File-Move Commands" in listing.output


def test_file_move_without_delete_source_exits_1(sim: ZfsSim) -> None:
    """Without --delete-source the command refuses on stderr before any zfs call."""
    result = runner.invoke(cli.app, ["file-move", "tank/data"])

    assert result.exit_code == 1
    assert "pass --delete-source to confirm" in result.stderr
    assert sim.runner.calls == []  # type: ignore[attr-defined]


@pytest.mark.usefixtures("sim")
def test_file_move_batch_size_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var sets the batch size; the default is 100G."""
    seen: list[tuple[int, bool]] = []

    def record(_zfs: object, _settings: object, _dataset: str, *, batch_size: int, delete_source: bool) -> list[str]:
        seen.append((batch_size, delete_source))
        return []

    monkeypatch.setattr(cli, "file_move", record)

    assert runner.invoke(cli.app, ["file-move", "tank/data", "--delete-source"]).exit_code == 0
    monkeypatch.setenv("ZFS_ENCRYPT_MIGRATE_BATCH_SIZE", "2K")
    assert runner.invoke(cli.app, ["file-move", "tank/data", "--delete-source"]).exit_code == 0

    assert seen == [(100 * 1024**3, True), (2048, True)]


@pytest.mark.parametrize("value", ["0", "0G", "junk"])
def test_file_move_rejects_bad_batch_size(sim: ZfsSim, value: str) -> None:
    """A zero or unparsable batch size is a usage error before zfs."""
    result = runner.invoke(cli.app, ["file-move", "tank/data", "--delete-source", "--batch-size", value])

    assert result.exit_code == 2  # noqa: PLR2004
    assert sim.runner.calls == []  # type: ignore[attr-defined]


@pytest.mark.usefixtures("sim")
def test_file_move_rejects_zero_batch_size_message() -> None:
    """The zero refusal names the rule."""
    result = runner.invoke(cli.app, ["file-move", "tank/data", "--delete-source", "--batch-size", "0"])

    assert "batch size must be above 0" in result.output.replace("\n", " ")


def test_file_move_rejects_bad_dataset_name(sim: ZfsSim) -> None:
    """A bad dataset name is a usage error before zfs."""
    result = runner.invoke(cli.app, ["file-move", "tank/da ta", "--delete-source"])

    assert result.exit_code == 2  # noqa: PLR2004
    assert sim.runner.calls == []  # type: ignore[attr-defined]


def test_file_move_interrupt_in_writer_probe_exits_130_with_remount_hint(
    sim: ZfsSim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C between the writer probe's unmount and remount exits 130 and says how to remount."""

    def interrupt_mount(args: Sequence[str]) -> CommandResult:
        if tuple(args[:2]) == ("zfs", "mount"):
            raise KeyboardInterrupt
        return sim.answer(args)

    def probe_only(zfs: Zfs, _settings: object, dataset: str, **_options: object) -> list[str]:
        probe_writers(zfs, dataset)
        return []

    sim.runner.responder = interrupt_mount  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "file_move", probe_only)

    result = runner.invoke(cli.app, ["file-move", "tank/data", "--delete-source"])

    assert result.exit_code == 130  # noqa: PLR2004
    assert "remount it with: zfs mount tank/data" in result.output
