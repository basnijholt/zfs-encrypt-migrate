"""Tests for file-move --check: every refusal at once, plus what only the check looks for, and nothing changes."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from zfs_encrypt_migrate import preflight
from zfs_encrypt_migrate.constants import MARK_ADMITTED, MARK_SOURCE, SCRATCH
from zfs_encrypt_migrate.errors import MigrationError
from zfs_encrypt_migrate.filemove import CheckReport, _run_lock, check_file_move

from .conftest import STAGE
from .fakeproc import CONTAINER, FakeProc, mount_line

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from zfs_encrypt_migrate.migrate import Settings

    from .conftest import FileMoveEnv

DATASET = "tank/media"
STAGED = f"{STAGE}/media"
IMMUTABLE = 0x10


@pytest.fixture
def proc(filemove_env: FileMoveEnv, tmp_path: Path) -> FakeProc:
    """Build a fake /proc whose checking process sees the simulated mounts."""
    return FakeProc(tmp_path / "proc", filemove_env.sim.mountinfo().decode())


def check(
    env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    dataset: str = DATASET,
) -> CheckReport:
    """Check the dataset with a private lock and the fake /proc."""
    return check_file_move(
        env.zfs,
        settings,
        dataset,
        batch_size=10**9,
        proc=proc.root,
        lock_path=tmp_path / "lock",
    )


def found(problems: list[MigrationError]) -> list[tuple[str, tuple[str, ...]]]:
    """Return each problem's message with its hints."""
    return [(problem.message, problem.hints) for problem in problems]


def assert_unchanged(env: FileMoveEnv, tmp_path: Path) -> None:
    """Nothing was written, mounted, unmounted, snapshotted, keyed or copied, and no lock file was created."""
    assert env.sim.ops() == []
    assert not [call for call in env.runner.calls if call[0] == "rsync" or "load-key" in call]
    assert not (tmp_path / "lock").exists()


def test_a_ready_dataset_has_nothing_to_report(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
) -> None:
    """With nothing in the way, the check is empty and leaves everything as it was."""
    report = check(filemove_env, settings, proc, tmp_path)
    assert (report.problems, report.notes) == ([], [])
    assert_unchanged(filemove_env, tmp_path)


def test_every_problem_is_reported_at_once(
    filemove_env: FileMoveEnv,
    settings: Settings,
    tmp_path: Path,
    key_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run stops at the first refusal; the check goes on, in the run's order, then adds what only it sees."""
    env = filemove_env
    sim = env.sim
    key_file.chmod(0o644)
    sim.datasets |= {
        f"{DATASET}/kid",
        f"{STAGE}/other",
        f"{STAGE}/old",
        f"{STAGE}/old/media",
        "tank/elsewhere",
        "tank/elsewhere/media",
    }
    sim.prop("tank/elsewhere/media", MARK_SOURCE, DATASET)
    sim.prop(DATASET, "readonly", "on")
    (env.src / SCRATCH).mkdir()
    sim.pool_props["tank", "altroot"] = "/mnt"
    for prop in ("reservation", "refreservation"):
        sim.prop(DATASET, prop, "1073741824")
    sim.prop(DATASET, "origin", "tank/old@base")
    sim.pool_props["tank", "checkpoint"] = "1052672"
    sim.prop(DATASET, "dedup", "on")
    sim.pool_props["tank", "dedup_table_size"] = "4096"
    sim.snapshot(f"{DATASET}@auto")
    sim.avail = settings.min_free
    sim.quotas[DATASET, "user"] = [("1000", "1073741824", "none")]
    monkeypatch.setattr(preflight, "_inode_flags", lambda path: IMMUTABLE if path.endswith("a.txt") else 0)
    snap = os.path.realpath(env.src / ".zfs" / "snapshot" / "auto")
    proc = FakeProc(tmp_path / "proc", sim.mountinfo().decode() + mount_line(200, 100, snap, f"{DATASET}@auto"))
    proc.add(2345, comm="indexer", ns=CONTAINER, fds=[env.src / "docs" / "b.txt"])

    report = check(env, settings, proc, tmp_path)

    messages = [problem.message for problem in report.problems]
    assert messages[:15] == [
        f"key file {key_file} must have mode 600",
        f"{DATASET} has child datasets; use the tree commands, or file-move each child",
        f"{DATASET} is read-only; file-move must delete its files",
        f"{env.src / SCRATCH} exists; file-move needs that name for its own files",
        "pool tank is imported with altroot=/mnt; file-move needs a normal import",
        (
            f"{DATASET} has reservation=1073741824, which keeps deleted space reserved; clear it with: "
            f"zfs set reservation=none {DATASET} (set it again on the new dataset after the move)"
        ),
        (
            f"{DATASET} has refreservation=1073741824, which keeps deleted space reserved; clear it with: "
            f"zfs set refreservation=none {DATASET} (set it again on the new dataset after the move)"
        ),
        (
            f"{DATASET} is a clone of tank/old@base; its origin keeps the space of deleted files; "
            f"promote it first: zfs promote {DATASET}"
        ),
        "pool tank has a checkpoint, which keeps deleted space allocated; discard it first: zpool checkpoint -d tank",
        f"{DATASET} has dedup=on; deleting deduplicated files frees no space, so file-move cannot run; see the docs",
        (
            f"pool tank has a dedup table (4.0K); deduplicated blocks shared with {DATASET} would not be freed, "
            "so file-move cannot run; see the docs"
        ),
        f"tank/elsewhere/media already holds a file-move of {DATASET}; rerun with --stage tank/elsewhere",
        f"{DATASET} has snapshots, which keep deleted files allocated: {DATASET}@auto",
        f"a migration is already in flight under {STAGE}",
        f"{STAGE}/old/media already exists",
    ]
    assert messages[15].startswith("free space on tank above the floor (0B) does not cover the worst case")
    assert messages[16:] == [
        (
            f"{DATASET} has files or directories with the immutable or append-only flag, which keeps file-move from "
            "deleting them or the files in them"
        ),
        f"snapshot {DATASET}@auto is mounted at {snap}, which keeps {DATASET} busy",
        f"PID 2345 (indexer) uses {DATASET} from another mount namespace: open file {env.src / 'docs' / 'b.txt'}",
    ]
    # Each says how to fix it: in its hints, or in its message as the run has always said it.
    inline = ("clear it with:", "promote it first:", "discard it first:", "see the docs", "tree commands", "rerun with")
    assert all(problem.hints or any(fix in problem.message for fix in inline) for problem in report.problems)
    assert [note.message for note in report.notes] == [
        f"{DATASET} has per-user, per-group or per-project quotas, which file-move does not carry over",
    ]
    assert_unchanged(env, tmp_path)


def wrong_mountpoint(env: FileMoveEnv, _key: Path) -> None:
    """Give the dataset a mountpoint whose trailing space zfs get strips."""
    env.sim.prop(DATASET, "mountpoint", f"{env.src} ")


FIXES: list[tuple[str, Callable[[FileMoveEnv, Path], object], str, list[tuple[str, tuple[str, ...]]]]] = [
    (
        "key mode",
        lambda _env, key: key.chmod(0o644),
        DATASET,
        [("key file {key} must have mode 600", ("chmod 600 {key}",))],
    ),
    (
        "key missing",
        lambda _env, key: key.unlink(),
        DATASET,
        [("key file {key} not found", ("write the passphrase into it, with mode 600",))],
    ),
    (
        "pool root",
        lambda _env, _key: None,
        "tank",
        [
            (
                "tank is a pool root; file-move is not supported",
                ("a pool root cannot be swapped; see Rebuilding a Whole Pool Encrypted in the guides",),
            ),
        ],
    ),
    (
        "other pool",
        lambda _env, _key: None,
        "other/media",
        [
            (
                "other/media is on pool other but the stage is on tank; file-move cannot rename across pools",
                ("use a stage on pool other",),
            ),
            ("dataset other/media does not exist", ("check the name with: zfs list",)),
        ],
    ),
    (
        "nested",
        lambda _env, _key: None,
        f"{STAGE}/x",
        [
            (
                f"{STAGE}/x overlaps the stage {STAGE}; neither may be or contain the other",
                (f"use a stage that does not overlap {STAGE}/x",),
            ),
            (f"dataset {STAGE}/x does not exist", ("check the name with: zfs list",)),
        ],
    ),
    (
        "volume",
        lambda env, _key: env.sim.volumes.add(DATASET),
        DATASET,
        [(f"{DATASET} is a volume; file-move moves the files of a filesystem", ("use seed and cutover",))],
    ),
    (
        "encrypted",
        lambda env, _key: env.sim.prop(DATASET, "encryption", "aes-256-gcm"),
        DATASET,
        [(f"{DATASET} is already encrypted", ("nothing to do",))],
    ),
    (
        "read-only",
        lambda env, _key: env.sim.prop(DATASET, "readonly", "on"),
        DATASET,
        [(f"{DATASET} is read-only; file-move must delete its files", (f"zfs set readonly=off {DATASET}",))],
    ),
    (
        "no mountpoint",
        lambda env, _key: env.sim.prop(DATASET, "mountpoint", "none"),
        DATASET,
        [
            (
                f"{DATASET} has mountpoint none; file-move needs a mounted path",
                (f"give it a mountpoint: zfs set mountpoint=PATH {DATASET}",),
            ),
        ],
    ),
    (
        "not mounted",
        lambda env, _key: env.sim.prop(DATASET, "mounted", "no"),
        DATASET,
        [(f"{DATASET} is not mounted", (f"zfs mount {DATASET}",))],
    ),
    (
        "mounted elsewhere",
        wrong_mountpoint,
        DATASET,
        [
            (
                "{src} is not where tank/media is mounted (nothing is mounted there)",
                (
                    (
                        f"findmnt --source {DATASET} shows where it is mounted; "
                        "give it a mountpoint without leading or "
                        "trailing spaces"
                    ),
                ),
            ),
        ],
    ),
    (
        "reserved name",
        lambda env, _key: (env.src / SCRATCH).mkdir(),
        DATASET,
        [("{src}/.zfs-encrypt-migrate exists; file-move needs that name for its own files", ("rename it",))],
    ),
    (
        "altroot",
        lambda env, _key: env.sim.pool_props.__setitem__(("tank", "altroot"), "/mnt"),
        DATASET,
        [
            (
                "pool tank is imported with altroot=/mnt; file-move needs a normal import",
                ("export the pool and import it without -R: zpool export tank, then zpool import tank",),
            ),
        ],
    ),
    (
        "foreign staged",
        lambda env, _key: env.sim.datasets.add(STAGED),
        DATASET,
        [
            (
                f"{STAGED} exists and is not a file-move of {DATASET}",
                ("finish or clear the migration that owns it first",),
            ),
        ],
    ),
    (
        "in flight",
        lambda env, _key: env.sim.datasets.add(f"{STAGE}/other"),
        DATASET,
        [(f"a migration is already in flight under {STAGE}", (f"finish the migration under {STAGE} first",))],
    ),
    (
        "old copy",
        lambda env, _key: env.sim.datasets.update({f"{STAGE}/old", f"{STAGE}/old/media"}),
        DATASET,
        [
            (
                f"{STAGE}/old/media already exists",
                (f"destroy it once you no longer need it (zfs destroy -r {STAGE}/old/media), or rename it",),
            ),
        ],
    ),
]


@pytest.mark.parametrize(
    ("setup", "dataset", "expected"),
    [case[1:] for case in FIXES],
    ids=[case[0] for case in FIXES],
)
def test_each_refusal_says_how_to_fix_it(  # noqa: PLR0917
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    key_file: Path,
    setup: Callable[[FileMoveEnv, Path], object],
    dataset: str,
    expected: list[tuple[str, tuple[str, ...]]],
) -> None:
    """A refusal whose message names no fix carries one as a hint; the run prints it too."""
    setup(filemove_env, key_file)
    fill = {"key": key_file, "src": filemove_env.src}
    want = [(message.format(**fill), tuple(hint.format(**fill) for hint in hints)) for message, hints in expected]
    assert found(check(filemove_env, settings, proc, tmp_path, dataset).problems) == want
    assert_unchanged(filemove_env, tmp_path)


def test_a_path_that_is_not_the_dataset_is_never_read(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the mountpoint zfs gives is not the dataset's mount, the check reads nothing there."""
    wrong_mountpoint(filemove_env, tmp_path)
    read: list[str] = []
    monkeypatch.setattr(preflight, "_inode_flags", lambda path: read.append(path) or 0)
    proc.add(2345, ns=CONTAINER, fds=[filemove_env.src / "a.txt"])
    report = check(filemove_env, settings, proc, tmp_path)
    assert len(report.problems) == 1
    assert read == []


def test_a_move_whose_worst_case_does_not_fit_is_reported(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
) -> None:
    """The run admits the move only after it created the encrypted dataset; the check does the sum up front."""
    filemove_env.sim.avail = settings.min_free
    report = check(filemove_env, settings, proc, tmp_path)
    assert found(report.problems) == [
        (
            (
                "free space on tank above the floor (0B) does not cover the worst case of this move (12.0K: largest "
                "batch or file 12.0K, block-cloned data 0B, lost compression 0B)"
            ),
            (
                (
                    "free space on tank, lower --batch-size if the largest batch is the biggest term, or remove clones "
                    "you no longer need (zpool get bcloneused,bclonesaved tank)"
                ),
            ),
        ),
    ]
    assert (STAGED, MARK_ADMITTED) not in filemove_env.sim.props
    assert_unchanged(filemove_env, tmp_path)


def test_an_admitted_move_is_not_summed_again(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
) -> None:
    """Once admitted, a rerun does not check the space again, so neither does the check."""
    env = filemove_env
    env.sim.datasets.add(STAGED)
    env.sim.prop(STAGED, MARK_SOURCE, DATASET)
    env.sim.prop(STAGED, MARK_ADMITTED, "yes")
    env.sim.avail = settings.min_free
    assert check(env, settings, proc, tmp_path).problems == []


def test_a_running_file_move_is_all_there_is_to_report(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
) -> None:
    """While a run holds the lock the check reads nothing, so it never keeps that run's unmount busy."""
    with _run_lock(tmp_path / "lock"):
        report = check(filemove_env, settings, proc, tmp_path)
    assert found(report.problems) == [
        (f"another file-move is running (lock {tmp_path / 'lock'})", ("check again once it has stopped",)),
    ]
    assert filemove_env.runner.calls == []


def test_no_run_starts_while_the_check_reads(
    filemove_env: FileMoveEnv,
    settings: Settings,
    proc: FakeProc,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check holds the lock shared, so a run that starts meanwhile refuses before it touches anything."""
    lock = tmp_path / "lock"
    lock.touch()
    refused: list[str] = []

    def start_a_run(_path: str) -> int:
        if not refused:
            with pytest.raises(MigrationError) as info, _run_lock(lock):
                pass
            refused.append(info.value.message)
        return 0

    monkeypatch.setattr(preflight, "_inode_flags", start_a_run)
    assert check(filemove_env, settings, proc, tmp_path).problems == []
    assert refused == [f"another file-move is running (lock {lock})"]
    # Released once the check is done.
    with _run_lock(lock):
        pass
