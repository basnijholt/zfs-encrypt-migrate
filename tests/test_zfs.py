"""Tests for the zfs wrapper."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import typer

from zfs_encrypt_migrate.constants import COMMAND_STARTUP_ERROR_RETURNCODE
from zfs_encrypt_migrate.errors import ZfsError
from zfs_encrypt_migrate.zfs import Prop, Zfs, validate_child, validate_name

from .conftest import FakeRunner, fail, ok
from .helpers import make
from .zfs_sim import ZfsSim

if TYPE_CHECKING:
    from zfs_encrypt_migrate.process import CommandResult


def test_exists_true_and_false() -> None:
    """A missing dataset is False; other failures raise."""
    runner = FakeRunner(queue=[ok("tank/a\n"), fail("cannot open 'tank/b': dataset does not exist\n")])
    zfs = Zfs(runner)

    assert zfs.exists("tank/a") is True
    assert zfs.exists("tank/b") is False
    assert runner.calls[0] == ("zfs", "list", "-H", "-o", "name", "tank/a")

    with pytest.raises(ZfsError, match="permission denied"):
        Zfs(FakeRunner(queue=[fail("permission denied\n")])).exists("tank/a")


def test_get_returns_trimmed_value() -> None:
    """Get reads one property as a parsable value."""
    runner = FakeRunner(queue=[ok("on\n")])

    assert Zfs(runner).get("canmount", "tank/a") == "on"
    assert runner.calls == [("zfs", "get", "-H", "-p", "-o", "value", "canmount", "tank/a")]


def test_subtree_lists_filesystems_and_volumes() -> None:
    """Subtree returns names in zfs list order."""
    runner = FakeRunner(queue=[ok("tank/a\ntank/a/b\n")])

    assert Zfs(runner).subtree("tank/a") == ["tank/a", "tank/a/b"]
    assert runner.calls == [("zfs", "list", "-rH", "-t", "filesystem,volume", "-o", "name", "-s", "name", "tank/a")]


def test_filesystems_lists_filesystems_only() -> None:
    """Filesystems leaves volumes out, so the swap never sets canmount on them."""
    runner = FakeRunner(queue=[ok("tank/a\tfilesystem\ntank/a/vol\tvolume\ntank/a/b\tfilesystem\n")])

    assert Zfs(runner).filesystems("tank/a") == ["tank/a", "tank/a/b"]
    assert runner.calls == [
        ("zfs", "list", "-rH", "-t", "filesystem,volume", "-o", "name,type", "-s", "name", "tank/a"),
    ]


def test_filesystems_of_a_volume_is_empty() -> None:
    """A volume named as the root lists without error and yields no filesystems."""
    runner = FakeRunner(queue=[ok("tank/vol\tvolume\n")])

    assert Zfs(runner).filesystems("tank/vol") == []


def test_snapshot_recursive_flag() -> None:
    """Snapshot adds -r only when asked."""
    runner = FakeRunner()
    zfs = Zfs(runner)

    zfs.snapshot("tank/a@s", recursive=True)
    zfs.snapshot("tank/a@s", recursive=False)

    assert runner.calls == [("zfs", "snapshot", "-r", "tank/a@s"), ("zfs", "snapshot", "tank/a@s")]


def test_send_recv_builds_pipeline() -> None:
    """send_recv pipes zfs send into zfs recv."""
    runner = FakeRunner()

    Zfs(runner).send_recv(["-R", "tank/a@s"], ["-u", "tank/stage/a"])

    assert runner.pipelines == [(("zfs", "send", "-R", "tank/a@s"), ("zfs", "recv", "-u", "tank/stage/a"))]


def test_send_recv_names_failing_side() -> None:
    """A failed send or recv raises ZfsError naming that side."""
    with pytest.raises(ZfsError, match=r"zfs send .* failed: boom"):
        Zfs(FakeRunner(queue=[fail("boom\n"), ok()])).send_recv(["x"], ["y"])
    with pytest.raises(ZfsError, match=r"zfs recv .* failed: bang"):
        Zfs(FakeRunner(queue=[ok(), fail("bang\n")])).send_recv(["x"], ["y"])


def test_simple_write_commands() -> None:
    """Each wrapper builds the exact zfs invocation."""
    runner = FakeRunner()
    zfs = Zfs(runner)

    zfs.rename("tank/a", "tank/old/a")
    zfs.set_prop("tank/a", "readonly", "on")
    zfs.inherit("tank/a", "readonly")
    zfs.create("tank/stage", {"canmount": "off"})
    zfs.mount("tank/a")
    zfs.load_key("tank/a")
    zfs.unload_key("tank/a")
    zfs.pool_sync("tank")

    assert runner.calls == [
        ("zfs", "rename", "-u", "tank/a", "tank/old/a"),
        ("zfs", "set", "readonly=on", "tank/a"),
        ("zfs", "inherit", "readonly", "tank/a"),
        ("zfs", "create", "-o", "canmount=off", "tank/stage"),
        ("zfs", "mount", "tank/a"),
        ("zfs", "load-key", "tank/a"),
        ("zfs", "unload-key", "-r", "tank/a"),
        ("zpool", "sync", "tank"),
    ]


def test_write_command_failure_raises() -> None:
    """Non-zero exit raises ZfsError."""
    with pytest.raises(ZfsError, match="zfs mount tank/a failed: nope"):
        Zfs(FakeRunner(queue=[fail("nope\n")])).mount("tank/a")


def test_mount_passes_temporary_options() -> None:
    """Mount options go comma-joined after -o, for the duration of this mount only."""
    runner = FakeRunner()
    Zfs(runner).mount("tank/a", ["noatime", "ro"])
    assert runner.calls == [("zfs", "mount", "-o", "noatime,ro", "tank/a")]


def test_unmount_reports_busy_without_force() -> None:
    """A busy dataset returns False; no -f is ever used; other errors raise."""
    runner = FakeRunner(queue=[ok(), fail("cannot unmount 'tank/a': pool or dataset is busy\n"), fail("weird\n")])
    zfs = Zfs(runner)

    assert zfs.unmount("tank/a") is True
    assert zfs.unmount("tank/a") is False
    with pytest.raises(ZfsError):
        zfs.unmount("tank/a")
    assert all(call == ("zfs", "unmount", "tank/a") for call in runner.calls)


def test_pool_avail() -> None:
    """pool_avail parses bytes."""
    runner = FakeRunner(queue=[ok("12345\n")])
    zfs = Zfs(runner)

    assert zfs.pool_avail("tank") == 12345  # noqa: PLR2004
    assert runner.calls == [("zfs", "list", "-Hp", "-o", "avail", "tank")]


@pytest.mark.parametrize("name", ["tank", "tank/a.b_c-d:e", "a/b/c"])
def test_validate_name_accepts(name: str) -> None:
    """Plain dataset names pass through unchanged."""
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["", "-r", "tank/a@s", "tank/a b", "tank\n", "tank/", "/tank", "tank//a"])
def test_validate_name_rejects(name: str) -> None:
    """Option-like, snapshot, whitespace and trailing-newline names are refused."""
    with pytest.raises(typer.BadParameter):
        validate_name(name)


def _pipeline_results(send: CommandResult, recv: CommandResult) -> Zfs:
    return Zfs(FakeRunner(queue=[send, recv]))


def test_send_recv_reports_recv_when_send_died_of_sigpipe() -> None:
    """A recv failure is reported with its stderr even though send was killed by SIGPIPE."""
    for code in (-13, 141):
        zfs = _pipeline_results(fail("", code), fail("cannot receive: out of space\n"))
        with pytest.raises(ZfsError, match=r"zfs recv .* failed: cannot receive: out of space") as info:
            zfs.send_recv(["x"], ["y"])
        assert "send" not in str(info.value).split("failed:")[1]


def test_send_recv_reports_send_failure_when_recv_sees_eof() -> None:
    """A real send failure is reported when recv succeeded on the truncated stream."""
    with pytest.raises(ZfsError, match=r"zfs send .* failed: no such snapshot"):
        _pipeline_results(fail("no such snapshot\n"), ok()).send_recv(["x"], ["y"])


def test_send_recv_includes_both_stderrs_when_both_fail() -> None:
    """A real send failure plus a recv failure reports both messages."""
    with pytest.raises(ZfsError) as info:
        _pipeline_results(fail("send broke\n"), fail("recv broke\n")).send_recv(["x"], ["y"])

    assert "recv broke" in str(info.value)
    assert "send broke" in str(info.value)


def test_children_lists_direct_children_only() -> None:
    """Children asks for depth 1 and drops the dataset itself."""
    runner = FakeRunner(queue=[ok("tank/data\ntank/data/a\ntank/data/vol\n")])

    assert Zfs(runner).children("tank/data") == ["tank/data/a", "tank/data/vol"]
    assert runner.calls == [("zfs", "list", "-rH", "-t", "filesystem,volume", "-o", "name", "-d", "1", "tank/data")]


@pytest.mark.parametrize("name", ["child", "a.b-c_d:e", "x1"])
def test_validate_child_accepts(name: str) -> None:
    """Plain direct child names pass."""
    assert validate_child(name) == name


@pytest.mark.parametrize("name", ["", "a/b", "a@b", "-x", "a b", "/a", "a/"])
def test_validate_child_rejects(name: str) -> None:
    """Empty names, paths, snapshots and option-like names are refused."""
    with pytest.raises(typer.BadParameter):
        validate_child(name)


def test_unload_key_retries_while_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A volume still held open by udev is retried; other failures raise at once."""
    monkeypatch.setattr("zfs_encrypt_migrate.zfs.time.sleep", lambda _: None)
    busy = fail("Key unload error: 'tank/a' is busy.\n")
    runner = FakeRunner(queue=[busy, busy, ok()])

    Zfs(runner).unload_key("tank/a")

    assert len(runner.calls) == 3  # noqa: PLR2004

    with pytest.raises(ZfsError, match="permission denied"):
        Zfs(FakeRunner(queue=[fail("permission denied\n")])).unload_key("tank/a")

    stuck = FakeRunner(queue=[busy] * 20)
    with pytest.raises(ZfsError, match="busy"):
        Zfs(stuck).unload_key("tank/a")
    assert len(stuck.calls) == 20  # noqa: PLR2004


def test_props_parses_value_and_source() -> None:
    """Every property comes back with its value and source, user properties included."""
    stdout = "compression\tlz4\tlocal\nrecordsize\t131072\tdefault\nuser:x\t1\tinherited from tank\n"
    runner = FakeRunner(queue=[ok(stdout)])

    assert Zfs(runner).props("tank/a") == {
        "compression": Prop("lz4", "local"),
        "recordsize": Prop("131072", "default"),
        "user:x": Prop("1", "inherited from tank"),
    }
    assert runner.calls == [("zfs", "get", "-H", "-p", "-o", "property,value,source", "all", "tank/a")]


def test_snapshots_lists_own_snapshots() -> None:
    """Only the dataset's own snapshots are listed, by full name."""
    runner = FakeRunner(queue=[ok("tank/a@one\ntank/a@two\n")])

    assert Zfs(runner).snapshots("tank/a") == ["tank/a@one", "tank/a@two"]
    assert runner.calls == [("zfs", "list", "-H", "-t", "snapshot", "-o", "name", "-d", "1", "tank/a")]


def test_check_key_attached_uses_run_attached() -> None:
    """An attached check goes through run_attached, not run."""
    runner = FakeRunner()

    assert Zfs(runner).check_key("tank/a", "prompt", attached=True) is True
    assert runner.calls == []
    assert runner.attached == [("zfs", "load-key", "-n", "-L", "prompt", "tank/a")]


def test_check_key_returns_false_on_failure() -> None:
    """A wrong key is False, not an error."""
    runner = FakeRunner(queue=[fail("Key load error\n")])

    assert Zfs(runner).check_key("tank/a", "file:///k", attached=False) is False
    assert runner.calls == [("zfs", "load-key", "-n", "-L", "file:///k", "tank/a")]


def test_check_key_raises_on_startup_error() -> None:
    """A zfs that cannot start is an error, not a wrong key."""
    runner = FakeRunner(queue=[fail("no zfs\n", returncode=COMMAND_STARTUP_ERROR_RETURNCODE)])

    with pytest.raises(ZfsError, match="no zfs"):
        Zfs(runner).check_key("tank/a", "prompt", attached=False)


def test_sim_props_snapshots_inherit_and_check_key() -> None:
    """The simulator answers the new commands the way zfs does."""
    sim = ZfsSim(["tank/a"])
    zfs, _ = make(sim)
    zfs.create("tank/b", {"compression": "lz4"})
    sim.inherited["tank/b", "user:x"] = ("1", "tank")
    sim.snapshot("tank/b@s1")
    sim.snapshot("tank/b/c@s2")

    props = zfs.props("tank/b")
    assert props["compression"] == Prop("lz4", "local")
    assert props["user:x"] == Prop("1", "inherited from tank")
    assert props["canmount"] == Prop("on", "default")
    assert zfs.snapshots("tank/b") == ["tank/b@s1"]

    zfs.inherit("tank/b", "compression")
    assert "compression" not in zfs.props("tank/b")

    assert zfs.check_key("tank/b", "prompt", attached=True) is True
    assert sim.props.get(("tank/b", "keystatus")) is None
