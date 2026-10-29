"""Errors that end a migration run with a clear message."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .process import CommandResult


class MigrationError(Exception):
    """A refusal or failure reported to the operator as one message plus recovery hints."""

    def __init__(self, message: str, hints: Sequence[str] = ()) -> None:
        """Store the message and the recovery hints."""
        super().__init__(message)
        self.message = message
        self.hints = tuple(hints)


class ZfsError(MigrationError):
    """A zfs or zpool command failed."""

    def __init__(self, result: CommandResult, args: Sequence[str], hints: Sequence[str] = ()) -> None:
        """Build the message from the failed command and its stderr."""
        detail = result.stderr.strip() or f"exit {result.returncode}"
        super().__init__(f"{shlex.join(args)} failed: {detail}", hints)
        self.result = result
        self.args_run = tuple(args)
