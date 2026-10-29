"""Command line interface."""

from __future__ import annotations

from typing import Annotated

import typer

from .audit import find_unencrypted
from .errors import MigrationError
from .output import console, print_error
from .process import CommandRunner, SubprocessRunner
from .version import __version__
from .zfs import Zfs, validate_name


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
