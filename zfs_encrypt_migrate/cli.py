"""Command line interface."""

from __future__ import annotations

from typing import Annotated

import typer

from .output import console
from .version import __version__


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


if __name__ == "__main__":
    app()
