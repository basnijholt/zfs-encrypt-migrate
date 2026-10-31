"""Tests for GitHub workflow invariants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load_workflow(name: str) -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text())
    return workflow


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML parses the bare `on:` key as boolean True.
    triggers: dict[str, Any] = workflow.get("on") or workflow.get(True) or {}  # type: ignore[call-overload]
    return triggers


def test_release_deploy_is_gated_on_tests() -> None:
    """The PyPI publish job must not run unless the full test suite passed."""
    jobs = _load_workflow("release.yml")["jobs"]
    assert jobs["test"]["uses"] == "./.github/workflows/pytest.yml"
    needs = jobs["deploy"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "test" in needs


def test_pytest_workflow_is_callable_from_release() -> None:
    """The test workflow must stay reusable so the release gate can call it."""
    assert "workflow_call" in _triggers(_load_workflow("pytest.yml"))


def test_nix_job_builds_flake_checks() -> None:
    """The nix job must build the checks this flake actually defines."""
    steps = _load_workflow("pytest.yml")["jobs"]["nix"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "nix build .#checks.x86_64-linux.module " in commands
    assert "nix build .#checks.x86_64-linux.integration " in commands
    assert "nix build .#checks.x86_64-linux.file-move " in commands
