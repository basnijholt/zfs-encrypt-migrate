"""Tests for the exported NixOS module."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def run_nix_eval(expr: str) -> subprocess.CompletedProcess[str]:
    """Run a Nix eval expression for module tests."""
    nix = shutil.which("nix")
    if nix is None:
        pytest.skip("nix is not installed")

    return subprocess.run(
        [
            nix,
            "--extra-experimental-features",
            "nix-command flakes",
            "eval",
            "--impure",
            "--json",
            "--expr",
            expr,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def eval_expr(options: str, result: str) -> str:
    """Build an expression that evaluates the module with `options` and returns `result`."""
    return f"""
      let
        flake = builtins.getFlake "path:{REPO}";
        system = builtins.currentSystem;
        eval = flake.inputs.nixpkgs.lib.nixosSystem {{
          inherit system;
          modules = [
            flake.nixosModules.default
            ({{
              system.stateVersion = "26.05";
              boot.loader.grub.enable = false;
              fileSystems."/" = {{ device = "none"; fsType = "tmpfs"; }};
              services.zfs-encrypt-migrate = {{ {options} }};
            }})
          ];
        }};
      in {result}
    """


def test_audit_unit_and_timer_are_generated() -> None:
    """The audit oneshot, its timer and OnFailure wiring come from the options."""
    expr = eval_expr(
        """
          enable = true;
          audit = {
            enable = true;
            pools = [ "tank" "ssd" ];
            exact = [ "tank/stage" ];
            subtrees = [ "ssd/docker" ];
            onFailure = [ "notify@%n.service" ];
          };
        """,
        """{
          service = let s = eval.config.systemd.services.zfs-encrypt-migrate-audit; in {
            inherit (s) wants after path unitConfig;
            serviceConfig = s.serviceConfig;
          };
          timer = let t = eval.config.systemd.timers.zfs-encrypt-migrate-audit; in {
            inherit (t) wantedBy timerConfig;
          };
          failed = builtins.filter (a: !a.assertion) eval.config.assertions;
          packages = map (p: p.name or "") eval.config.environment.systemPackages;
        }""",
    )
    result = run_nix_eval(expr)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["failed"] == []
    assert any(name.startswith("zfs-encrypt-migrate") for name in data["packages"])

    service = data["service"]
    assert service["serviceConfig"]["Type"] == "oneshot"
    assert service["unitConfig"]["OnFailure"] == ["notify@%n.service"]
    assert "zfs-import.target" in service["wants"]
    assert "zfs-import.target" in service["after"]
    assert any("zfs-" in str(p) for p in service["path"])
    exec_start = service["serviceConfig"]["ExecStart"]
    assert exec_start.endswith("audit --exact tank/stage --subtree ssd/docker tank ssd")

    timer = data["timer"]
    assert timer["wantedBy"] == ["timers.target"]
    assert timer["timerConfig"]["OnCalendar"] == "daily"
    assert timer["timerConfig"]["Persistent"] is True


def test_audit_requires_pools() -> None:
    """Enabling the audit without pools is an assertion failure."""
    expr = eval_expr(
        "enable = true; audit.enable = true;",
        "map (a: a.message) (builtins.filter (a: !a.assertion) eval.config.assertions)",
    )
    result = run_nix_eval(expr)

    assert result.returncode == 0, result.stderr
    messages = json.loads(result.stdout)
    assert any(m.startswith("services.zfs-encrypt-migrate.audit.pools must") for m in messages)


def test_disabled_module_defines_nothing() -> None:
    """The module is inert unless enabled."""
    expr = eval_expr(
        "",
        'builtins.hasAttr "zfs-encrypt-migrate-audit" eval.config.systemd.services',
    )
    result = run_nix_eval(expr)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) is False


def test_audit_works_without_enable() -> None:
    """The audit unit and timer exist with `enable = false`; `enable` only installs the CLI."""
    expr = eval_expr(
        """
          package = flake.packages.${system}.default;
          audit = { enable = true; pools = [ "tank" ]; };
        """,
        """{
          service = builtins.hasAttr "zfs-encrypt-migrate-audit" eval.config.systemd.services;
          timer = builtins.hasAttr "zfs-encrypt-migrate-audit" eval.config.systemd.timers;
          failed = builtins.filter (a: !a.assertion) eval.config.assertions;
          installed = builtins.any (p: (p.pname or "") == "zfs-encrypt-migrate") eval.config.environment.systemPackages;
        }""",
    )
    result = run_nix_eval(expr)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {"service": True, "timer": True, "failed": [], "installed": False}
