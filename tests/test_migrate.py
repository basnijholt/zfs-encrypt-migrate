"""Tests for seed, catchup and cutover against a simulated zfs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate.errors import Interrupted, MigrationError
from zfs_encrypt_migrate.migrate import (
    Settings,
    abort_guard,
    catchup,
    cutover,
    dirty_since,
    in_flight,
    seed,
    snaps_match,
    unmount_subtree,
)
from zfs_encrypt_migrate.zfs import Zfs

from .conftest import FakeRunner, fail
from .zfs_sim import ZfsSim, mutations

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from zfs_encrypt_migrate.process import CommandResult

STAGE = "tank/stage"
MSG = "x"


def recv_args(key_file: Path, target: str, keyformat: str = "passphrase") -> tuple[str, ...]:
    """Return the exact recv arguments of a seed."""
    return (
        "zfs",
        "recv",
        "-u",
        "-o",
        "encryption=aes-256-gcm",
        "-o",
        f"keyformat={keyformat}",
        "-o",
        f"keylocation=file://{key_file}",
        target,
    )


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """Create a private key file."""
    path = tmp_path / "key"
    path.write_text("secret")
    path.chmod(0o600)
    return path


@pytest.fixture
def settings(key_file: Path) -> Settings:
    """Build settings with a tiny free-space floor."""
    return Settings(stage=STAGE, key_file=key_file, min_free=1000, keyformat="passphrase", final_keylocation="prompt")


@pytest.fixture
def sim() -> ZfsSim:
    """Build a pool with a dataset that has one child."""
    simulator = ZfsSim(["tank", "tank/data", "tank/data/child"])
    simulator.roots = ["tank/data", f"{STAGE}/data"]
    for name in ("tank/data", "tank/data/child"):
        simulator.prop(name, "mounted", "yes")
    return simulator


def make(
    sim: ZfsSim,
    responder: Callable[[Sequence[str]], CommandResult | None] | None = None,
    after: Callable[[Sequence[str]], None] | None = None,
) -> tuple[Zfs, FakeRunner]:
    """Build a Zfs over a runner answered by the simulator.

    ``responder`` may answer a command itself (returning None defers to the simulator);
    ``after`` runs once the simulator answered, to tamper with its state.
    """

    def answer(args: Sequence[str]) -> CommandResult:
        if responder is not None:
            result = responder(args)
            if result is not None:
                return result
        result = sim.answer(args)
        if after is not None:
            after(args)
        return result

    runner = FakeRunner(answer)
    return Zfs(runner), runner


def fail_on(*prefix: str) -> Callable[[Sequence[str]], CommandResult | None]:
    """Fail every command that starts with the given words."""

    def responder(args: Sequence[str]) -> CommandResult | None:
        if tuple(args[: len(prefix)]) == prefix:
            return fail("injected\n")
        return None

    return responder


def staged_ready(sim: ZfsSim, *, final: bool = False) -> None:
    """Stage state after a seed: encrypted staged tree with the key unloaded."""
    sim.datasets |= {STAGE, f"{STAGE}/old", f"{STAGE}/data", f"{STAGE}/data/child"}
    for name in ("tank/data", "tank/data/child"):
        sim.snapshot(f"{name}@enc-seed")
        sim.snapshot(f"{name.replace('tank/data', f'{STAGE}/data')}@enc-seed")
    for name in (f"{STAGE}/data", f"{STAGE}/data/child"):
        sim.prop(name, "encryptionroot", f"{STAGE}/data")
        sim.prop(name, "encryption", "aes-256-gcm")
        sim.prop(name, "keystatus", "unavailable")
    if final:
        for name in ("tank/data", "tank/data/child", f"{STAGE}/data", f"{STAGE}/data/child"):
            sim.snapshot(f"{name}@enc-final")


# --- seed ---


def test_seed_exact_commands(sim: ZfsSim, settings: Settings, key_file: Path) -> None:
    """Seed creates containers, snapshots, sends and unloads the key, in that order."""
    zfs, runner = make(sim)

    assert seed(zfs, settings, "tank/data") == []

    assert runner.pipelines == [(("zfs", "send", "-R", "tank/data@enc-seed"), recv_args(key_file, f"{STAGE}/data"))]
    assert [c for c in mutations(runner.calls) if c[1] != "recv"] == [
        ("zfs", "create", "-o", "canmount=off", STAGE),
        ("zfs", "create", "-o", "canmount=off", f"{STAGE}/old"),
        ("zfs", "snapshot", "-r", "tank/data@enc-seed"),
        ("zfs", "unload-key", "-r", f"{STAGE}/data"),
    ]


def test_seed_uses_keyformat_setting(sim: ZfsSim, settings: Settings, key_file: Path) -> None:
    """The keyformat option reaches the recv arguments."""
    zfs, runner = make(sim)
    hex_settings = Settings(STAGE, key_file, settings.min_free, "hex", "prompt")

    seed(zfs, hex_settings, "tank/data")

    assert runner.pipelines[0][1] == recv_args(key_file, f"{STAGE}/data", "hex")


def test_seed_skips_existing_containers(sim: ZfsSim, settings: Settings) -> None:
    """Existing stage and old containers are not recreated."""
    sim.datasets |= {STAGE, f"{STAGE}/old"}
    zfs, runner = make(sim)

    seed(zfs, settings, "tank/data")

    assert not [c for c in runner.calls if c[1] == "create"]


def test_seed_allows_pool_root(settings: Settings, key_file: Path) -> None:
    """A pool root seeds into stage/<pool>."""
    simulator = ZfsSim(["ssd", "ssd/docker", "tank"])
    zfs, runner = make(simulator)

    seed(zfs, settings, "ssd")

    assert runner.pipelines[0][1] == recv_args(key_file, f"{STAGE}/ssd")


def test_seed_refuses_missing_key_file(sim: ZfsSim, settings: Settings, key_file: Path) -> None:
    """No key file refuses before any zfs command."""
    key_file.unlink()
    zfs, runner = make(sim)

    with pytest.raises(MigrationError, match=f"key file {key_file} not found"):
        seed(zfs, settings, "tank/data")
    assert runner.calls == []


def test_seed_refuses_key_directory(sim: ZfsSim, settings: Settings, key_file: Path) -> None:
    """A directory is not a key file."""
    key_file.unlink()
    key_file.mkdir()
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match="not found"):
        seed(zfs, settings, "tank/data")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o700])
def test_seed_refuses_wrong_key_mode(sim: ZfsSim, settings: Settings, key_file: Path, mode: int) -> None:
    """Only mode exactly 600 passes."""
    key_file.chmod(mode)
    zfs, runner = make(sim)

    with pytest.raises(MigrationError, match=f"key file {key_file} must have mode 600"):
        seed(zfs, settings, "tank/data")
    assert runner.calls == []


def test_seed_refuses_missing_dataset(sim: ZfsSim, settings: Settings) -> None:
    """A nonexistent dataset is refused."""
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match="dataset tank/nope does not exist"):
        seed(zfs, settings, "tank/nope")


def test_seed_refuses_encrypted(sim: ZfsSim, settings: Settings) -> None:
    """An already encrypted dataset is refused."""
    sim.prop("tank/data", "encryption", "aes-256-gcm")
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match="tank/data is already encrypted"):
        seed(zfs, settings, "tank/data")


def test_seed_refuses_in_flight(sim: ZfsSim, settings: Settings) -> None:
    """A staged tree of another migration blocks a new seed."""
    sim.datasets |= {STAGE, f"{STAGE}/old", f"{STAGE}/other", "tank/two"}
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match=f"a migration is already in flight under {STAGE}"):
        seed(zfs, settings, "tank/two")


def test_in_flight_ignores_stage_old_and_old_copies(sim: ZfsSim, settings: Settings) -> None:
    """Only trees outside stage/old count; prefix lookalikes do count."""
    sim.datasets |= {STAGE, f"{STAGE}/old", f"{STAGE}/old/x", f"{STAGE}/old/x/y"}
    zfs, _ = make(sim)
    assert in_flight(zfs, settings) == []

    sim.datasets.add(f"{STAGE}/older")
    assert in_flight(zfs, settings) == [f"{STAGE}/older"]


def test_in_flight_without_stage(sim: ZfsSim, settings: Settings) -> None:
    """A missing stage means nothing is in flight."""
    zfs, _ = make(sim)
    assert in_flight(zfs, settings) == []


@pytest.mark.parametrize("existing", [f"{STAGE}/data", f"{STAGE}/old/data"])
def test_seed_refuses_existing_target(sim: ZfsSim, settings: Settings, existing: str) -> None:
    """A staged or old copy of the same leaf is refused."""
    sim.datasets |= {STAGE, f"{STAGE}/old", existing}
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match=f"{existing} already exists|already in flight"):
        seed(zfs, settings, "tank/data")


def test_seed_refuses_old_copy_exists(sim: ZfsSim, settings: Settings) -> None:
    """An old copy blocks the leaf even though it is not in flight."""
    sim.datasets |= {STAGE, f"{STAGE}/old", f"{STAGE}/old/data"}
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match=f"{STAGE}/old/data already exists"):
        seed(zfs, settings, "tank/data")


def test_seed_refuses_below_free_space_floor(sim: ZfsSim, settings: Settings) -> None:
    """Avail minus used below the floor is refused with the numbers."""
    sim.avail = 1050
    zfs, _ = make(sim)

    with pytest.raises(
        MigrationError,
        match=r"free space on tank \(1050\) minus size of tank/data \(100\) is below floor 1000",
    ):
        seed(zfs, settings, "tank/data")


def test_seed_free_space_exactly_at_floor_passes(sim: ZfsSim, settings: Settings) -> None:
    """The floor itself is allowed."""
    sim.avail = 1100
    zfs, _ = make(sim)

    seed(zfs, settings, "tank/data")


def test_seed_free_space_uses_stage_pool(sim: ZfsSim, settings: Settings) -> None:
    """Free space is read from the pool of the stage."""
    zfs, runner = make(sim)

    seed(zfs, settings, "tank/data")

    assert ("zfs", "list", "-Hp", "-o", "avail", "tank") in runner.calls


# --- key unload on failure ---


def recv_creates_loaded_key(sim: ZfsSim) -> Callable[[Sequence[str]], CommandResult | None]:
    """Fail recv after it created the staged dataset with its key loaded."""

    def responder(args: Sequence[str]) -> CommandResult | None:
        if args[1] == "recv":
            sim.datasets.add(args[-1])
            sim.prop(args[-1], "keystatus", "available")
            return fail("cannot receive: out of space\n")
        return None

    return responder


def test_failed_seed_unloads_key(sim: ZfsSim, settings: Settings) -> None:
    """A failing recv leaves the staged key unloaded."""
    zfs, runner = make(sim, recv_creates_loaded_key(sim))

    with pytest.raises(MigrationError, match="out of space"):
        seed(zfs, settings, "tank/data")

    assert ("zfs", "unload-key", "-r", f"{STAGE}/data") in runner.calls
    assert sim.props[f"{STAGE}/data", "keystatus"] == "unavailable"


def test_failed_seed_without_staged_dataset_skips_unload(sim: ZfsSim, settings: Settings) -> None:
    """Nothing to unload when the staged dataset was never created."""
    zfs, runner = make(sim, fail_on("zfs", "snapshot"))

    with pytest.raises(MigrationError, match="injected"):
        seed(zfs, settings, "tank/data")

    assert not [c for c in runner.calls if c[1] == "unload-key"]


def test_unload_failure_never_masks_original_error(sim: ZfsSim, settings: Settings) -> None:
    """A failing unload becomes a hint; the original error stays."""
    inner = recv_creates_loaded_key(sim)

    def responder(args: Sequence[str]) -> CommandResult | None:
        if args[1] == "unload-key":
            return fail("busy\n")
        return inner(args)

    zfs, _ = make(sim, responder)

    with pytest.raises(MigrationError, match="out of space") as info:
        seed(zfs, settings, "tank/data")

    assert f"could not unload key; run: zfs unload-key -r {STAGE}/data" in info.value.hints


def test_keyboard_interrupt_mid_pipeline_unloads_key(sim: ZfsSim, settings: Settings) -> None:
    """Ctrl-C during send | recv unloads the staged key and surfaces as Interrupted."""

    class Interrupting(FakeRunner):
        def pipeline(self, first: Sequence[str], second: Sequence[str]) -> tuple[CommandResult, CommandResult]:
            self.pipelines.append((tuple(first), tuple(second)))
            sim.datasets.add(second[-1])
            sim.prop(second[-1], "keystatus", "available")
            raise KeyboardInterrupt

    runner = Interrupting(sim.answer)

    with pytest.raises(Interrupted) as info:
        seed(Zfs(runner), settings, "tank/data")

    assert isinstance(info.value.__cause__, KeyboardInterrupt)
    assert ("zfs", "unload-key", "-r", f"{STAGE}/data") in runner.calls


def test_unexpected_exception_is_wrapped_and_key_unloaded(sim: ZfsSim, settings: Settings) -> None:
    """Non-MigrationError exceptions become MigrationError after cleanup."""

    class Exploding(FakeRunner):
        def pipeline(self, _first: Sequence[str], second: Sequence[str]) -> tuple[CommandResult, CommandResult]:
            sim.datasets.add(second[-1])
            sim.prop(second[-1], "keystatus", "available")
            msg = "kaboom"
            raise RuntimeError(msg)

    runner = Exploding(sim.answer)

    with pytest.raises(MigrationError, match="RuntimeError: kaboom"):
        seed(Zfs(runner), settings, "tank/data")

    assert ("zfs", "unload-key", "-r", f"{STAGE}/data") in runner.calls


# --- catchup ---


def test_catchup_exact_commands(sim: ZfsSim, settings: Settings) -> None:
    """Catch-up loads the key, snapshots, sends -R -I, receives -u -x encryption, unloads."""
    staged_ready(sim)
    zfs, runner = make(sim)

    assert catchup(zfs, settings, "tank/data") == []

    assert runner.pipelines == [
        (
            ("zfs", "send", "-R", "-I", "@enc-seed", "tank/data@enc-final"),
            ("zfs", "recv", "-u", "-x", "encryption", f"{STAGE}/data"),
        ),
    ]
    assert [c for c in mutations(runner.calls) if c[1] != "recv"] == [
        ("zfs", "load-key", f"{STAGE}/data"),
        ("zfs", "snapshot", "-r", "tank/data@enc-final"),
        ("zfs", "unload-key", "-r", f"{STAGE}/data"),
    ]


def test_catchup_skips_load_when_key_loaded(sim: ZfsSim, settings: Settings) -> None:
    """An already loaded key is not loaded again."""
    staged_ready(sim)
    sim.prop(f"{STAGE}/data", "keystatus", "available")
    zfs, runner = make(sim)

    catchup(zfs, settings, "tank/data")

    assert not [c for c in runner.calls if c[1] == "load-key"]


def test_catchup_requires_staged(sim: ZfsSim, settings: Settings) -> None:
    """Without a staged tree, run seed first."""
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match=f"{STAGE}/data does not exist; run seed first"):
        catchup(zfs, settings, "tank/data")


def test_catchup_requires_seed_snapshot(sim: ZfsSim, settings: Settings) -> None:
    """Without enc-seed on the source, run seed first."""
    staged_ready(sim)
    del sim.snapshots["tank/data@enc-seed"]
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match="tank/data@enc-seed does not exist; run seed first"):
        catchup(zfs, settings, "tank/data")


@pytest.mark.parametrize(
    ("stale", "expected"),
    [
        (["tank/data"], ["zfs destroy -r tank/data@enc-final"]),
        ([f"{STAGE}/data"], [f"zfs destroy -r {STAGE}/data@enc-final"]),
        (
            ["tank/data", f"{STAGE}/data"],
            ["zfs destroy -r tank/data@enc-final", f"zfs destroy -r {STAGE}/data@enc-final"],
        ),
    ],
)
def test_catchup_refuses_stale_final(sim: ZfsSim, settings: Settings, stale: list[str], expected: list[str]) -> None:
    """An existing enc-final on either side is refused with every destroy hint, key unloaded."""
    staged_ready(sim)
    sim.prop(f"{STAGE}/data", "keystatus", "available")
    for name in stale:
        sim.snapshot(f"{name}@enc-final")
    zfs, runner = make(sim)

    with pytest.raises(MigrationError) as info:
        catchup(zfs, settings, "tank/data")

    assert all(line in info.value.message for line in expected)
    assert runner.pipelines == []
    assert sim.props[f"{STAGE}/data", "keystatus"] == "unavailable"


def test_catchup_refuses_guid_mismatch(sim: ZfsSim, settings: Settings) -> None:
    """Differing enc-final GUIDs after the send are an error and the key is unloaded."""
    staged_ready(sim)
    zfs, runner = make(
        sim,
        after=lambda args: (
            sim.snapshots.update({f"{STAGE}/data/child@enc-final": "other"}) if args[1] == "recv" else None
        ),
    )
    sim.prop(f"{STAGE}/data", "keystatus", "available")

    with pytest.raises(MigrationError, match=f"enc-final snapshots of tank/data and {STAGE}/data differ"):
        catchup(zfs, settings, "tank/data")

    assert ("zfs", "unload-key", "-r", f"{STAGE}/data") in runner.calls


def test_catchup_refuses_child_not_encrypted_under_root(sim: ZfsSim, settings: Settings) -> None:
    """A child that stayed unencrypted is caught by the recursive encryptionroot check."""
    staged_ready(sim)
    zfs, _ = make(
        sim,
        after=lambda args: sim.prop(f"{STAGE}/data/child", "encryptionroot", "-") if args[1] == "recv" else None,
    )

    with pytest.raises(MigrationError, match=f"{STAGE}/data/child is not encrypted under {STAGE}/data"):
        catchup(zfs, settings, "tank/data")


# --- cutover ---


def test_cutover_refuses_pool_root(settings: Settings) -> None:
    """Pool roots cannot be cut over; nothing runs."""
    zfs, runner = make(ZfsSim(["ssd"]))

    with pytest.raises(MigrationError, match="ssd is a pool root; cutover is not supported"):
        cutover(zfs, settings, "ssd")
    assert runner.calls == []


def test_cutover_requires_staged(sim: ZfsSim, settings: Settings) -> None:
    """Without a staged tree, run seed first."""
    zfs, _ = make(sim)

    with pytest.raises(MigrationError, match=f"{STAGE}/data does not exist; run seed first") as info:
        cutover(zfs, settings, "tank/data")
    assert info.value.hints == ()


def test_cutover_skips_catchup_when_finals_match(sim: ZfsSim, settings: Settings) -> None:
    """Matching enc-final snapshots mean no new send, and the swap order is exact."""
    staged_ready(sim, final=True)
    sim.prop(f"{STAGE}/data/child", "canmount", "noauto")
    zfs, runner = make(sim)

    lines = cutover(zfs, settings, "tank/data")

    assert runner.pipelines == []
    assert lines == [f"cutover done; once verified, free the old copy with: zfs destroy -r {STAGE}/old/data"]
    assert mutations(runner.calls) == [
        ("zfs", "load-key", f"{STAGE}/data"),
        ("zfs", "unmount", "tank/data/child"),
        ("zfs", "unmount", "tank/data"),
        ("zpool", "sync", "tank"),
        ("zfs", "set", "canmount=noauto", "tank/data"),
        ("zfs", "set", "canmount=noauto", "tank/data/child"),
        ("zfs", "rename", "-u", "tank/data", f"{STAGE}/old/data"),
        ("zfs", "set", "readonly=on", f"{STAGE}/old/data"),
        ("zfs", "rename", "-u", f"{STAGE}/data", "tank/data"),
        ("zfs", "set", "keylocation=prompt", "tank/data"),
        ("zfs", "mount", "tank/data"),
    ]


def test_cutover_uses_final_keylocation_setting(sim: ZfsSim, key_file: Path) -> None:
    """The final keylocation setting reaches the swap."""
    staged_ready(sim, final=True)
    zfs, runner = make(sim)

    cutover(zfs, Settings(STAGE, key_file, 1000, "passphrase", "file:///etc/key"), "tank/data")

    assert ("zfs", "set", "keylocation=file:///etc/key", "tank/data") in runner.calls


def test_cutover_runs_catchup_when_finals_missing(sim: ZfsSim, settings: Settings) -> None:
    """Without matching finals, cutover catches up first."""
    staged_ready(sim)
    zfs, runner = make(sim)

    cutover(zfs, settings, "tank/data")

    assert len(runner.pipelines) == 1
    assert runner.pipelines[0][0] == ("zfs", "send", "-R", "-I", "@enc-seed", "tank/data@enc-final")
    assert ("zfs", "rename", "-u", "tank/data", f"{STAGE}/old/data") in runner.calls


def test_cutover_diverged_finals_fall_back_to_catchup_and_refuse(sim: ZfsSim, settings: Settings) -> None:
    """A diverged enc-final pair is not trusted; the stale-final refusal fires."""
    staged_ready(sim, final=True)
    sim.snapshots["tank/data/child@enc-final"] = "other"
    sim.prop(f"{STAGE}/data", "keystatus", "available")
    zfs, runner = make(sim)

    with pytest.raises(MigrationError, match="already exists"):
        cutover(zfs, settings, "tank/data")

    assert sim.props[f"{STAGE}/data", "keystatus"] == "unavailable"
    assert runner.pipelines == []


def test_cutover_swap_skips_legacy_and_none_mountpoints(sim: ZfsSim, settings: Settings) -> None:
    """Only canmount=on datasets with a real mountpoint that are not mounted get mounted."""
    staged_ready(sim, final=True)
    sim.prop(f"{STAGE}/data/child", "mountpoint", "legacy")
    zfs, runner = make(sim)

    cutover(zfs, settings, "tank/data")

    assert [c for c in runner.calls if c[1] == "mount"] == [("zfs", "mount", "tank/data")]


def test_cutover_unmounts_children_first(sim: ZfsSim) -> None:
    """unmount_subtree goes deepest first and skips datasets that are not mounted."""
    sim.datasets.add("tank/data/child/leaf")
    sim.prop("tank/data/child/leaf", "mounted", "yes")
    sim.prop("tank/data/child", "mounted", "no")
    zfs, runner = make(sim)

    unmount_subtree(zfs, "tank/data")

    assert [c for c in runner.calls if c[1] == "unmount"] == [
        ("zfs", "unmount", "tank/data/child/leaf"),
        ("zfs", "unmount", "tank/data"),
    ]


def test_cutover_busy_unmount_refuses_with_remount_hint(sim: ZfsSim, settings: Settings) -> None:
    """A busy dataset refuses the cutover, unloads the key and hints how to remount."""
    staged_ready(sim, final=True)
    sim.busy.add("tank/data")
    zfs, runner = make(sim)

    with pytest.raises(MigrationError, match="cannot unmount tank/data; a writer is probably still running") as info:
        cutover(zfs, settings, "tank/data")

    assert info.value.hints == (
        "cutover aborted.",
        "tank/data may be unmounted; remount with: zfs mount -a",
    )
    assert ("zfs", "unload-key", "-r", f"{STAGE}/data") in runner.calls
    assert not [c for c in runner.calls if c[1] == "unmount" and "-f" in c]


def test_cutover_refuses_writes_since_catchup(sim: ZfsSim, settings: Settings) -> None:
    """Writes after enc-final list the datasets and both destroy hints; nothing is swapped."""
    staged_ready(sim, final=True)
    sim.prop("tank/data/child", "written@enc-final", "4096")
    zfs, runner = make(sim)

    with pytest.raises(MigrationError) as info:
        cutover(zfs, settings, "tank/data")

    assert info.value.message == "written since catchup:\ntank/data/child"
    assert info.value.hints[:3] == (
        "drop the stale snapshots, then retry cutover:",
        "  zfs destroy -r tank/data@enc-final",
        f"  zfs destroy -r {STAGE}/data@enc-final",
    )
    assert info.value.hints[3:] == ("cutover aborted.", "tank/data may be unmounted; remount with: zfs mount -a")
    assert ("zpool", "sync", "tank") in runner.calls
    assert not [c for c in runner.calls if c[1] == "rename"]
    assert sim.props[f"{STAGE}/data", "keystatus"] == "unavailable"


def test_cutover_never_destroys(sim: ZfsSim, settings: Settings) -> None:
    """No command of a full cutover is a destroy."""
    staged_ready(sim)
    zfs, runner = make(sim)

    cutover(zfs, settings, "tank/data")

    assert not [c for c in runner.calls if c[1] == "destroy"]


def test_cutover_success_does_not_unload_key(sim: ZfsSim, settings: Settings) -> None:
    """After a successful swap the guard leaves the new dataset alone (no failure, no unload)."""
    staged_ready(sim, final=True)
    zfs, runner = make(sim)

    cutover(zfs, settings, "tank/data")

    assert not [c for c in runner.calls if c[1] == "unload-key"]


# --- phase hints ---


@pytest.mark.parametrize(
    ("failing", "expected"),
    [
        (
            ("zfs", "set", "canmount=noauto", "tank/data/child"),
            (
                "cutover aborted.",
                "tank/data is unmounted and canmount may be noauto; restore with:",
                "  zfs set canmount=on tank/data",
                "  zfs set canmount=on tank/data/child",
                "  zfs mount -a",
            ),
        ),
        (
            ("zfs", "rename", "-u", "tank/data"),
            (
                "cutover aborted.",
                "tank/data is unmounted and canmount may be noauto; restore with:",
                "  zfs set canmount=on tank/data",
                "  zfs set canmount=on tank/data/child",
                "  zfs mount -a",
            ),
        ),
        (
            ("zfs", "set", "readonly=on"),
            (
                "cutover aborted.",
                f"the original is at {STAGE}/old/data (canmount=noauto, readonly=on); put it back with:",
                f"  zfs rename -u {STAGE}/old/data tank/data",
                "  zfs inherit readonly tank/data",
                "  zfs set canmount=on tank/data",
                "  zfs set canmount=on tank/data/child",
                "  zfs mount -a",
            ),
        ),
        (
            ("zfs", "rename", "-u", f"{STAGE}/data"),
            (
                "cutover aborted.",
                f"the original is at {STAGE}/old/data (canmount=noauto, readonly=on); put it back with:",
                f"  zfs rename -u {STAGE}/old/data tank/data",
                "  zfs inherit readonly tank/data",
                "  zfs set canmount=on tank/data",
                "  zfs set canmount=on tank/data/child",
                "  zfs mount -a",
            ),
        ),
        (
            ("zfs", "set", "keylocation=prompt"),
            (
                "cutover aborted.",
                "the swap completed; only the final steps failed. Finish with:",
                "  zfs set keylocation=prompt tank/data",
                "  zfs mount -a",
            ),
        ),
        (
            ("zfs", "mount", "tank/data"),
            (
                "cutover aborted.",
                "the swap completed; only the final steps failed. Finish with:",
                "  zfs set keylocation=prompt tank/data",
                "  zfs mount -a",
            ),
        ),
    ],
)
def test_phase_hints_follow_swap_progress(
    sim: ZfsSim,
    settings: Settings,
    failing: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    """Each swap failure prints the hint for how far the swap got."""
    staged_ready(sim, final=True)
    zfs, _ = make(sim, fail_on(*failing))

    with pytest.raises(MigrationError, match="injected") as info:
        cutover(zfs, settings, "tank/data")

    assert info.value.hints == expected


def test_canmount_hint_lists_only_datasets_that_were_on(sim: ZfsSim, settings: Settings) -> None:
    """Datasets already noauto are not listed for restoration."""
    staged_ready(sim, final=True)
    sim.prop("tank/data/child", "canmount", "noauto")
    zfs, _ = make(sim, fail_on("zfs", "rename", "-u", "tank/data"))

    with pytest.raises(MigrationError) as info:
        cutover(zfs, settings, "tank/data")

    assert "  zfs set canmount=on tank/data/child" not in info.value.hints
    assert "  zfs set canmount=on tank/data" in info.value.hints


def test_renamed_new_hint_uses_final_keylocation(sim: ZfsSim, key_file: Path) -> None:
    """The final hint names the configured keylocation."""
    staged_ready(sim, final=True)
    zfs, _ = make(sim, fail_on("zfs", "mount", "tank/data"))

    with pytest.raises(MigrationError) as info:
        cutover(zfs, Settings(STAGE, key_file, 1000, "passphrase", "file:///k"), "tank/data")

    assert "  zfs set keylocation=file:///k tank/data" in info.value.hints


def test_abort_guard_label_and_no_leaf(sim: ZfsSim, settings: Settings) -> None:
    """A guard with a custom label and no leaf prints the unmounted hint under that label."""
    zfs, _ = make(sim)

    with (
        pytest.raises(MigrationError) as info,
        abort_guard(zfs, settings, f"{STAGE}/data", source="tank/data/child", label="tree-move"),
    ):
        raise MigrationError(MSG, ["first"])

    assert info.value.hints == (
        "first",
        "tree-move aborted.",
        "tank/data/child may be unmounted; remount with: zfs mount -a",
    )


def test_abort_guard_without_source_adds_no_hints(sim: ZfsSim, settings: Settings) -> None:
    """Seed and catch-up guards only unload the key."""
    zfs, _ = make(sim)

    with pytest.raises(MigrationError) as info, abort_guard(zfs, settings, f"{STAGE}/data"):
        raise MigrationError(MSG)

    assert info.value.hints == ()


def test_abort_guard_success_does_nothing(sim: ZfsSim, settings: Settings) -> None:
    """No exception means no commands."""
    zfs, runner = make(sim)

    with abort_guard(zfs, settings, f"{STAGE}/data"):
        pass

    assert runner.calls == []


# --- helpers ---


def test_dirty_since_lists_written_datasets(sim: ZfsSim) -> None:
    """Datasets with written@snap other than 0 are returned."""
    sim.prop("tank/data", "written@s", "1")
    zfs, _ = make(sim)

    assert dirty_since(zfs, "tank/data", "s") == ["tank/data"]


def test_snaps_match_false_when_snapshot_missing(sim: ZfsSim) -> None:
    """A missing snapshot on either side is a mismatch, not an error."""
    staged_ready(sim)
    zfs, _ = make(sim)

    assert snaps_match(zfs, "tank/data", f"{STAGE}/data", "enc-seed") is True
    assert snaps_match(zfs, "tank/data", f"{STAGE}/data", "enc-final") is False
    sim.snapshot("tank/data@enc-final")
    sim.snapshot("tank/data/child@enc-final")
    assert snaps_match(zfs, "tank/data", f"{STAGE}/data", "enc-final") is False
