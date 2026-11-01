"""Console output helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:
    from collections.abc import Sequence

console = Console()
_err_console = Console(stderr=True)


def print_error(message: str, hints: Sequence[str] = ()) -> None:
    """Print a failure line followed by its recovery hints."""
    _err_console.print(f"[red]error:[/red] {escape(message)}", soft_wrap=True)
    for hint in hints:
        _err_console.print(escape(hint), soft_wrap=True)
