---
icon: lucide/rocket
---

# Getting Started

## Install

Install `zfs-encrypt-migrate` on the ZFS host.

=== "uv tool (Recommended)"

    ```bash
    uv tool install zfs-encrypt-migrate
    ```

=== "pip"

    ```bash
    pip install zfs-encrypt-migrate
    ```

=== "Nix"

    ```bash
    nix run github:basnijholt/zfs-encrypt-migrate -- --help
    ```

=== "From source"

    ```bash
    git clone https://github.com/basnijholt/zfs-encrypt-migrate
    cd zfs-encrypt-migrate
    uv sync --dev
    ```

On NixOS, the flake's module installs it for you; see [NixOS module options](usage.md#nixos-module-options).

## Requirements

- **Root.** Every command runs `zfs` and `zpool`.
- **The host's own `zfs` and `zpool` on `PATH`.** The package doesn't bundle them, so they always match the loaded kernel module.
- **`zfs rename -u`**, which renames a dataset without remounting it. The tool is tested on OpenZFS 2.4.
- **`rsync` 3.1.3 or newer, with ACL and xattr support, on `PATH`,** for `file-move` only. The Nix package appends one to `PATH`.
- **Free space on the same pool** for one dataset at a time, plus the `--min-free` floor (default `1T`).
- **On NixOS, no boot-time key loading for the stage.** Before the first `seed`, set `boot.zfs.requestEncryptionCredentials` to `false` or to a list without the stage, and keep it that way until the stage is empty. Otherwise a reboot mid-migration makes the pool import fail with `Key load error`; see [NixOS boot prompts](guides.md#nixos-boot-prompts).

## Choose a Stage

The stage is a dataset that holds the encrypted copy while it's being built, and the original once it's been swapped out.
It must be on the same pool as the datasets you migrate, because the swap is a rename.
The tool creates it (and `<stage>/old`) on first use, with `canmount=off`.

The stage is required and never guessed.
Pass `--stage` to every migration command, or set it once:

```bash
export ZFS_ENCRYPT_MIGRATE_STAGE=tank/stage
```

Every option has an environment variable like this; see [shared options](usage.md#shared-options).

## Create the Key File

`seed` reads the new key from a key file that must be a regular file with mode `600`.
The default is `/run/zfs-encrypt-migrate.key`; change it with `--key-file`.

```bash
install -m 600 /dev/null /run/zfs-encrypt-migrate.key
read -rs passphrase && printf '%s' "$passphrase" > /run/zfs-encrypt-migrate.key && unset passphrase
```

With the default `--keyformat passphrase`, OpenZFS wants 8 to 512 bytes.
`--keyformat hex` (64 hex characters) and `--keyformat raw` (32 bytes) work too.

Three things to know about this file:

- `seed` records it on the staged copy as `keylocation=file:///run/zfs-encrypt-migrate.key`, and later commands load the key from there, so keep the file in place until the cutover.
- Keep it on a tmpfs such as `/run`, which a reboot empties, so nothing at boot can unlock the staged copy (on NixOS, also see the [requirements](#requirements)). After a reboot mid-migration, write the same passphrase back before you continue.
- `cutover` sets `keylocation=prompt` (or your `--final-keylocation`) on the migrated dataset. From then on, this passphrase is what unlocks it after a reboot, so store it somewhere safe.

## Your First Migration

Seed the dataset.
It stays mounted and in use while the copy runs:

```bash
zfs-encrypt-migrate seed tank/data
```

This snapshots `tank/data@enc-seed` (recursively), sends the whole tree into `tank/stage/data` as a new `aes-256-gcm` encryption root, and unloads that key again.
`tank/stage/data` keeps every mountpoint that was set locally under `tank/data`, which is why its key stays unloaded: it must never mount over the original.

When you're ready for the short downtime, stop everything that writes to `tank/data` and run the cutover:

```bash
zfs-encrypt-migrate cutover tank/data
```

```text
verify the key with: zfs load-key -n tank/data
cutover done; once verified, free the old copy with: zfs destroy -r tank/stage/old/data
```

`cutover` unmounts `tank/data` (refusing if it's busy), sends what changed since the seed, syncs the pool and refuses if anything was still written.
Then it moves the original to `tank/stage/old/data` (read-only, `canmount=noauto`), renames the encrypted copy to `tank/data`, sets the final keylocation and mounts it.

If a step refuses, the error says why and what to run; fix that and run the same command again.
For example, if something wrote to `tank/data` after a separate `catchup`, `cutover` prints two `zfs rename -r` commands that move the stale `@enc-final` snapshots aside.

## Verify

```bash
# Encrypted, its own encryption root, mounted
zfs get -r encryption,encryptionroot,keylocation,keystatus,mounted tank/data

# Your snapshots came along, plus @enc-seed and @enc-final
zfs list -r -t snapshot -o name tank/data

# The original: unencrypted, read-only, not mounted
zfs get -r encryption,readonly,canmount,mounted tank/stage/old/data

# Your passphrase opens it: asks for it and checks it, without changing anything
zfs load-key -n tank/data
```

Start your services again, and make sure the passphrase is stored somewhere safe.

Destroying the old copy is the point of no return: from then on, the encrypted dataset is the only copy.
Once you're satisfied, free the space; the tool never destroys anything itself:

```bash
zfs destroy -r tank/stage/old/data
```

The `@enc-seed` and `@enc-final` snapshots stay on `tank/data` until you destroy them.

## Audit What's Left

```bash
zfs-encrypt-migrate audit --exact tank --subtree tank/stage tank
```

`audit` prints every unencrypted filesystem and volume of the named pools that isn't allowlisted, and exits 1 if there are any.
`--exact tank` allows the pool root (unless you created the pool encrypted), and `--subtree tank/stage` allows the stage, whose old copies are unencrypted by design.

## Next Steps

- [Usage](usage.md): every command, option, message and recovery hint.
- [Guides](guides.md): one dataset end to end, big trees with the tree commands, datasets bigger than the free space with `file-move`, rebuilding a whole pool encrypted, unlocking after reboot with `zfs-unlock`, and sanoid/syncoid.
