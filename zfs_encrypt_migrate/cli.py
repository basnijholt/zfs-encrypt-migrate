"""Command line interface."""

from __future__ import annotations

import signal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from .audit import find_unencrypted
from .errors import Interrupted, MigrationError
from .migrate import INTERRUPT_SIGNALS, Settings, catchup, cutover, seed
from .output import console, print_error
from .process import CommandRunner, SubprocessRunner
from .sizes import parse_size
from .tree import tree_finish, tree_move, tree_seed
from .version import __version__
from .zfs import Zfs, validate_child, validate_name

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType


def _version_callback(value: bool) -> None:  # noqa: FBT001
    if value:
        console.print(f"zfs-encrypt-migrate {__version__}")
        raise typer.Exit


app = typer.Typer(
    help="Migrate OpenZFS datasets to native encryption in place, one at a time",
    no_args_is_help=False,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _runner() -> CommandRunner:
    return SubprocessRunner()


def _validate_size(value: str) -> str:
    try:
        parse_size(value)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    return value


def _validate_keylocation(value: str) -> str:
    # zfs only rejects a bad keylocation at the final swap step, after both renames.
    scheme, _, rest = value.partition("://")
    if value == "prompt" or (scheme == "file" and rest.startswith("/") and len(rest) > 1):
        return value
    if scheme in {"http", "https"} and rest:
        return value
    msg = f"invalid keylocation {value!r}: use prompt, file:///absolute/path or an http(s):// URL"
    raise typer.BadParameter(msg)


def _validate_name(value: str) -> str:
    return validate_name(value)


def _validate_child(value: str) -> str:
    return validate_child(value)


StageOption = Annotated[
    str,
    typer.Option(
        "--stage",
        envvar="ZFS_ENCRYPT_MIGRATE_STAGE",
        help="Staging dataset, e.g. tank/stage",
        callback=_validate_name,
    ),
]
KeyFileOption = Annotated[
    Path,
    typer.Option(
        "--key-file",
        envvar="ZFS_ENCRYPT_MIGRATE_KEY_FILE",
        help="Key file for the staged tree; must have mode 600",
    ),
]
MinFreeOption = Annotated[
    str,
    typer.Option(
        "--min-free",
        envvar="ZFS_ENCRYPT_MIGRATE_MIN_FREE",
        help="Free space to keep on the staging pool after the copy, e.g. 500G or 1T",
        callback=_validate_size,
        metavar="SIZE",
    ),
]


class Keyformat(StrEnum):
    """Key formats accepted by zfs for a key file."""

    passphrase = "passphrase"  # noqa: S105
    hex = "hex"
    raw = "raw"


KeyformatOption = Annotated[
    Keyformat,
    typer.Option(
        "--keyformat",
        envvar="ZFS_ENCRYPT_MIGRATE_KEYFORMAT",
        help="Key format of the key file: passphrase, hex or raw",
    ),
]
FinalKeylocationOption = Annotated[
    str,
    typer.Option(
        "--final-keylocation",
        envvar="ZFS_ENCRYPT_MIGRATE_FINAL_KEYLOCATION",
        help="Keylocation set on the dataset after the swap, e.g. prompt or file:///path",
        callback=_validate_keylocation,
    ),
]
DatasetArgument = Annotated[str, typer.Argument(help="Dataset to migrate", callback=_validate_name, show_default=False)]
ChildArgument = Annotated[
    str,
    typer.Argument(help="Name of a direct child of DATASET", callback=_validate_child, show_default=False),
]


def _run_migration(
    command: Callable[[Zfs, Settings, str], list[str]],
    settings: Settings,
    dataset: str,
) -> None:
    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    # A dropped ssh session (SIGHUP) or a kill (SIGTERM) must unload the staged key like Ctrl-C does.
    # A signal that was ignored on entry (nohup) stays ignored, so a protected run survives a hangup.
    previous = {
        number: signal.signal(number, interrupt)
        for number in INTERRUPT_SIGNALS
        if signal.getsignal(number) is not signal.SIG_IGN
    }
    try:
        lines = command(Zfs(_runner()), settings, dataset)
    except Interrupted as error:
        print_error(error.message, error.hints)
        raise typer.Exit(130) from error
    except MigrationError as error:
        print_error(error.message, error.hints)
        raise typer.Exit(1) from error
    except KeyboardInterrupt as error:
        print_error("interrupted")
        raise typer.Exit(130) from error
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    for line in lines:
        console.print(line, markup=False, highlight=False, soft_wrap=True)


def _validate_names(values: list[str] | None) -> list[str] | None:
    return None if values is None else [validate_name(value) for value in values]


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[  # noqa: ARG001
        bool | None,
        typer.Option("--version", "-v", help="Show version and exit", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    """Migrate OpenZFS datasets to native encryption."""
    if ctx.invoked_subcommand is not None:
        return
    console.print(ctx.get_help())


@app.command("seed", rich_help_panel="Migration Commands")
def seed_command(
    dataset: DatasetArgument,
    *,
    stage: StageOption,
    key_file: KeyFileOption = Path("/run/zfs-encrypt-migrate.key"),
    min_free: MinFreeOption = "1T",
    keyformat: KeyformatOption = Keyformat.passphrase,
    final_keylocation: FinalKeylocationOption = "prompt",
) -> None:
    """Copy a dataset tree to an encrypted staged tree, then unload its key."""
    _run_migration(seed, Settings(stage, key_file, parse_size(min_free), keyformat.value, final_keylocation), dataset)


@app.command("catchup", rich_help_panel="Migration Commands")
def catchup_command(
    dataset: DatasetArgument,
    *,
    stage: StageOption,
    key_file: KeyFileOption = Path("/run/zfs-encrypt-migrate.key"),
    min_free: MinFreeOption = "1T",
    keyformat: KeyformatOption = Keyformat.passphrase,
    final_keylocation: FinalKeylocationOption = "prompt",
) -> None:
    """Send what changed since the seed to the staged tree."""
    _run_migration(
        catchup,
        Settings(stage, key_file, parse_size(min_free), keyformat.value, final_keylocation),
        dataset,
    )


@app.command("cutover", rich_help_panel="Migration Commands")
def cutover_command(
    dataset: DatasetArgument,
    *,
    stage: StageOption,
    key_file: KeyFileOption = Path("/run/zfs-encrypt-migrate.key"),
    min_free: MinFreeOption = "1T",
    keyformat: KeyformatOption = Keyformat.passphrase,
    final_keylocation: FinalKeylocationOption = "prompt",
) -> None:
    """Swap the staged tree into place; the original stays read-only under STAGE/old."""
    _run_migration(
        cutover,
        Settings(stage, key_file, parse_size(min_free), keyformat.value, final_keylocation),
        dataset,
    )


@app.command("tree-seed", rich_help_panel="Tree Commands")
def tree_seed_command(
    dataset: DatasetArgument,
    *,
    stage: StageOption,
    key_file: KeyFileOption = Path("/run/zfs-encrypt-migrate.key"),
    min_free: MinFreeOption = "1T",
    keyformat: KeyformatOption = Keyformat.passphrase,
    final_keylocation: FinalKeylocationOption = "prompt",
) -> None:
    """Copy only the parent to an encrypted staged dataset; its children move one by one."""
    _run_migration(
        tree_seed,
        Settings(stage, key_file, parse_size(min_free), keyformat.value, final_keylocation),
        dataset,
    )


@app.command("tree-move", rich_help_panel="Tree Commands")
def tree_move_command(
    dataset: DatasetArgument,
    child: ChildArgument,
    *,
    stage: StageOption,
    key_file: KeyFileOption = Path("/run/zfs-encrypt-migrate.key"),
    min_free: MinFreeOption = "1T",
    keyformat: KeyformatOption = Keyformat.passphrase,
    final_keylocation: FinalKeylocationOption = "prompt",
) -> None:
    """Copy one child subtree under the staged parent; the source stays until you destroy it."""
    _run_migration(
        lambda zfs, settings, parent: tree_move(zfs, settings, parent, child),
        Settings(stage, key_file, parse_size(min_free), keyformat.value, final_keylocation),
        dataset,
    )


@app.command("tree-finish", rich_help_panel="Tree Commands")
def tree_finish_command(
    dataset: DatasetArgument,
    *,
    stage: StageOption,
    key_file: KeyFileOption = Path("/run/zfs-encrypt-migrate.key"),
    min_free: MinFreeOption = "1T",
    keyformat: KeyformatOption = Keyformat.passphrase,
    final_keylocation: FinalKeylocationOption = "prompt",
) -> None:
    """Send the parent's last changes and swap the staged tree into place once no children remain."""
    _run_migration(
        tree_finish,
        Settings(stage, key_file, parse_size(min_free), keyformat.value, final_keylocation),
        dataset,
    )


@app.command(rich_help_panel="Audit Commands")
def audit(
    pools: Annotated[
        list[str] | None,
        typer.Argument(help="Pools to audit", callback=_validate_names, show_default=False),
    ] = None,
    exact: Annotated[
        list[str] | None,
        typer.Option("--exact", help="Allow this dataset by whole name (repeatable)", callback=_validate_names),
    ] = None,
    subtree: Annotated[
        list[str] | None,
        typer.Option("--subtree", help="Allow this dataset and its descendants (repeatable)", callback=_validate_names),
    ] = None,
) -> None:
    """List unencrypted datasets outside the allowlists; exit 1 if there are any."""
    if not pools:
        msg = "name at least one pool to audit"
        raise typer.BadParameter(msg, param_hint="POOLS")
    try:
        offenders = find_unencrypted(Zfs(_runner()), pools, exact or [], subtree or [])
    except MigrationError as error:
        print_error(error.message, error.hints)
        raise typer.Exit(1) from error
    for name in offenders:
        console.print(name, markup=False, highlight=False, soft_wrap=True)
    if offenders:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
