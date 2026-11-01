---
icon: lucide/git-pull-request
---

# Contributing

Contributions are welcome.

## Development Setup

Clone the repository:

```bash
git clone https://github.com/basnijholt/zfs-encrypt-migrate
cd zfs-encrypt-migrate
```

Install development dependencies:

```bash
uv sync --dev
```

## Running Tests

```bash
uv run pytest
```

The unit tests drive a simulated `zfs`, so they never touch a real pool.
The NixOS VM test runs every command against real OpenZFS on loopback pools inside a VM (x86_64-linux, with KVM):

```bash
nix build .#checks.x86_64-linux.integration -L
```

`nix/file-move-test.nix` is a second VM test for `file-move`: it moves a dataset bigger than its pool's free space, survives an interrupt and a rerun, and checks the result:

```bash
nix build .#checks.x86_64-linux.file-move -L
```

Never point the CLI at real pools while developing: it renames and unmounts datasets.

## Code Quality

Run linting:

```bash
uv run ruff check .
uv run ruff format --check .
```

Run type checking:

```bash
uv run mypy zfs_encrypt_migrate
```

Run Nix checks:

```bash
nix flake check --all-systems
```

## Documentation

Install docs dependencies and build the site:

```bash
uv sync --group docs
uv run zensical build
```

The generated site is written to `site/`.
GitHub Pages deploys that directory from the `Documentation` workflow on `main`.

The CLI help in `README.md` is generated; regenerate it after changing commands or options:

```bash
uvx --with . markdown-code-runner README.md
```

## Project Structure

```text
zfs_encrypt_migrate/              # Python package
zfs_encrypt_migrate/cli.py        # Typer command registration, shared options and exit codes
zfs_encrypt_migrate/migrate.py    # seed, catchup, cutover, the swap and the abort guard
zfs_encrypt_migrate/tree.py       # tree-seed, tree-move and tree-finish
zfs_encrypt_migrate/filemove.py   # file-move: batches, verification and the swap
zfs_encrypt_migrate/audit.py      # Unencrypted-dataset audit
zfs_encrypt_migrate/zfs.py        # zfs/zpool wrapper and dataset-name validation
zfs_encrypt_migrate/process.py    # Command runner and the send | recv pipeline
zfs_encrypt_migrate/sizes.py      # --min-free size parsing
nix/nixos-module.nix              # NixOS module with the audit service and timer
nix/integration-test.nix          # NixOS VM test against real OpenZFS
nix/file-move-test.nix            # NixOS VM test for file-move
tests/                            # Unit, CLI, packaging, workflow and NixOS module tests
docs/                             # Zensical documentation site
```

## Release Checklist

1. Merge the changes to `main`.
2. Publish a GitHub release tagged `vX.Y.Z`. hatch-vcs takes the package version from that tag; there is no version file to update.
3. Confirm the `Upload Python Package` workflow publishes to PyPI.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
