"""Helpers shared by the migration and tree tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zfs_encrypt_migrate.zfs import Zfs

from .conftest import FakeRunner, fail

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from zfs_encrypt_migrate.process import CommandResult

    from .zfs_sim import ZfsSim


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
