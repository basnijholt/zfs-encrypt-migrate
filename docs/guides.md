---
icon: lucide/book-open
---

# Guides

All commands here run as root, with the stage set in the environment:

```bash
export ZFS_ENCRYPT_MIGRATE_STAGE=tank/stage
```

## Migrating One Dataset

This walks `tank/media` onto encryption: a dataset that a service writes to and that is exported over NFS.

**1. Seed while it's in use.**
Create the [key file](getting-started.md#create-the-key-file), then:

```bash
zfs-encrypt-migrate seed tank/media
```

The seed sends the whole dataset, so it can take hours.
Over SSH, run it in tmux or screen, or under `nohup`: a dropped connection sends SIGHUP, which the tool treats like Ctrl-C, aborting the seed.
Under `nohup` the hangup stays ignored and the seed carries on.

If a seed is interrupted or fails, clean up before you run it again: the tool never destroys anything, so the `@enc-seed` snapshots on `tank/media` (`zfs destroy -r tank/media@enc-seed`), and any partial `tank/stage/media`, stay behind and make the next `seed` fail until they're gone.

If the host reboots before the cutover, the default key file on `/run` is gone.
Write the same passphrase back to the same path before you continue; the staged copy loads its key from there.

**2. Stop the writers.**
Stop the services that write to `tank/media` and anything else holding it open, such as the NFS export.
`cutover` unmounts the dataset and refuses with `cannot unmount ...; a writer is probably still running` if it's busy; it never forces.

**3. Cut over.**

```bash
zfs-encrypt-migrate cutover tank/media
```

This sends what changed since the seed, refuses if anything was still written, and swaps the encrypted copy into place.
The downtime is the time that last catch-up and the swap take, usually short after a recent seed.

`catchup` is optional.
It sends the same changes without unmounting anything, so you can check the incremental step beforehand; but anything written after it makes `cutover` refuse until you drop the two `@enc-final` snapshots it prints.
If you use it, run it after stopping the writers, right before `cutover`.

**4. Bring it back.**
Start the services, re-export the share, and remount it on the NFS clients: they see stale file handles, because the path is now a different dataset.

**5. Clean up.**
Once you're satisfied, free the old copy:

```bash
zfs destroy -r tank/stage/old/media
```

Then move on to the next dataset.
Old copies don't block the next `seed`, but a dataset with the same last component does: `tank/a/media` would also stage as `tank/stage/media` and needs `tank/stage/old/media` gone first.

Datasets on another pool need a stage on that pool, because the swap is a rename: for `ssd/docker`, use `--stage ssd/stage`.

## Moving a Big Tree

`seed` copies a dataset with all its children, so it needs room for the whole tree at once.
For a parent like `tank/backups` with several large children (`host1`, `host2`, ...), the tree commands move one child at a time instead, so you only ever need room for the parent's own data plus one child.

**1. Seed only the parent.**

```bash
zfs-encrypt-migrate tree-seed tank/backups
```

This copies `tank/backups` itself (not its children) to an encrypted `tank/stage/backups` and unloads its key.
Until `tree-finish`, this counts as the migration in flight under the stage.

**2. Move each child, then destroy its source.**
Stop whatever writes to the child first: `tree-move` unmounts it, and the child stays offline until `tree-finish`.

```bash
zfs-encrypt-migrate tree-move tank/backups host1
```

```text
moved tank/backups/host1; once verified, free it with: zfs destroy -r tank/backups/host1
```

The tool has already compared the `@enc-tree` snapshot GUIDs on both sides and checked that the copy is encrypted under `tank/stage/backups`.
Check that `tank/stage/backups/host1` is listed, then destroy the source to make room for the next child:

```bash
zfs list -r -o name,used tank/stage/backups
zfs destroy -r tank/backups/host1
```

Don't wait long: the unmounted source keeps its `canmount`, so `zfs mount -a` or a reboot mounts it again, and anything written to it then is not in the copy.

Repeat for every child.
Children keep their properties and snapshots, including a `mountpoint=none` or `readonly=on` they had, and volumes move the same way.

**3. Finish the parent.**
Stop whatever writes to `tank/backups`, then:

```bash
zfs-encrypt-migrate tree-finish tank/backups
```

`tree-finish` refuses with `children remain under tank/backups; ...` until every child has been moved and destroyed.
It then sends the parent's last changes and swaps `tank/stage/backups`, with the moved children under it, into place, exactly like `cutover`.
Afterwards, every child has `tank/backups` as its encryption root.

**4. Clean up.**

```bash
zfs destroy -r tank/stage/old/backups
```

Only the parent has an old copy.
The children you destroyed in step 2 have none, so from step 2 on the encrypted copy of each child is the only one: keep your backups current before you start.

## Rebuilding a Whole Pool Encrypted

A pool root can't be swapped (`cutover` refuses it), and creating the pool encrypted is the only way to make the root dataset itself encrypted.
The recipe below stages the whole pool `ssd` on another pool, `tank`, recreates `ssd` encrypted and sends the data back.
It's rehearsed step for step in the project's NixOS VM test (`nix/integration-test.nix`).

You need room on `tank` for everything on `ssd`, plus the `--min-free` floor.
The send-back moves the pool root's **children**; anything stored directly in the root dataset of `ssd` isn't part of it, so move such files into a child first.

**1. Seed and catch up the pool.**
`seed` and `catchup` accept a pool root and a stage on another pool:

```bash
zfs-encrypt-migrate seed ssd
```

Stop everything that writes to `ssd` (`catchup` doesn't unmount anything), then:

```bash
zfs-encrypt-migrate catchup ssd
```

`tank/stage/ssd` now holds an encrypted copy of the whole pool, up to `@enc-final`, with its key unloaded.
Anything written to `ssd` from here on is lost when the pool is destroyed; this read-only check should print a `0` for every dataset:

```bash
zpool sync ssd
zfs get -rH -t filesystem,volume -o name,value written@enc-final ssd
```

**2. Destroy and recreate the pool encrypted.**

> [!CAUTION]
> This destroys the pool. `zpool destroy` is irreversible: after it, the only copy of the data on `ssd` is `tank/stage/ssd` (and your backups). Check the staged copy before you continue.

```bash
udevadm settle
zpool destroy ssd
zpool create -O encryption=aes-256-gcm -O keyformat=passphrase \
  -O keylocation=file:///run/zfs-encrypt-migrate.key \
  -O mountpoint=/ssd ssd DEVICES...
```

Recreate it with your original vdev layout and pool properties; only the encryption options are new.
`udevadm settle` lets udev finish with any volumes on the pool first.
The recipe uses the staged copy's key file for the new pool root too, so one passphrase covers both.

**3. Send each child back.**

```bash
for d in $(zfs list -H -d1 -t filesystem,volume -o name tank/stage/ssd | tail -n +2); do
  c=${d#tank/stage/}
  zfs send -R -w "$d@enc-final" | zfs recv -u "$c" &&
    zfs load-key -L file:///run/zfs-encrypt-migrate.key "$c" &&
    zfs change-key -i "$c"
done
```

For each direct child of `tank/stage/ssd`, say `tank/stage/ssd/docker`:

- `zfs send -R -w` sends it raw: the blocks stay encrypted as they are, so nothing is decrypted or re-encrypted and the staged key never needs loading.
- `zfs recv -u ssd/docker` receives it, with all its descendants, snapshots and properties, without mounting. It arrives as its own encryption root, still wrapped by the staged key.
- `zfs load-key -L file://...` loads that key, from the same key file.
- `zfs change-key -i` makes it inherit the key of the pool root `ssd`, so one passphrase unlocks the whole pool.

**4. Switch the pool to its final keylocation and mount.**

```bash
zfs set keylocation=prompt ssd
zfs mount -a
```

**5. Verify.**
Every dataset on the pool should have `ssd` as its encryption root; this prints nothing when that holds:

```bash
zfs get -rH -o name,value encryptionroot ssd | awk '$2 != "ssd"'
```

The received snapshots keep their GUIDs, so you can compare any of them with the staged side:

```bash
zfs get -H -o value guid ssd/docker@enc-final tank/stage/ssd/docker@enc-final
```

**6. Clean up.**
Once you're satisfied:

```bash
zfs destroy -r tank/stage/ssd
```

## Unlocking After Reboot

`cutover` sets `keylocation=prompt` on the migrated dataset (unless you passed `--final-keylocation`), so after a reboot it stays locked, and unmounted, until its key is loaded.
The services that use it have to wait for that.

By hand:

```bash
zfs load-key tank/media   # asks for the passphrase
zfs mount -a
```

Each dataset you migrated with `cutover` or `tree-finish` is its own encryption root and needs its own `load-key`; its children come along.
After a [pool rebuild](#rebuilding-a-whole-pool-encrypted), the pool root is the only encryption root.

### With zfs-unlock

[`zfs-unlock`](https://zfs-unlock.nijho.lt) automates this without storing the passphrases on the storage host: a separate small device keeps them and sends them over a restricted SSH channel whenever the host comes up.
It loads keys with `zfs load-key -L prompt` and the passphrase on stdin, so it works with the default `--final-keylocation prompt` and the default `--keyformat passphrase`.

List each encryption root in the unlock device's config, with the passphrase from your key file:

```yaml
datasets:
  tank/media: ~/.secrets/tank-media-key
  tank/backups: ~/.secrets/tank-backups-key
```

and in the receiver's allowlist on the host, for example with its NixOS module:

```nix
services.zfs-unlock.receiver.datasets = [
  "tank/media"
  "tank/backups"
];
```

See the [zfs-unlock docs](https://zfs-unlock.nijho.lt/getting-started/) for the full setup.

### NixOS boot prompts

On NixOS, importing a pool asks for every `prompt` key it finds by default (`boot.zfs.requestEncryptionCredentials = true`) and waits for an answer indefinitely (`boot.zfs.passwordTimeout = 0`).
If something else, like `zfs-unlock`, loads these keys, set `boot.zfs.requestEncryptionCredentials` to `false`, or to a list of only the datasets that must unlock at boot.

With the default `true`, the same import step also runs `zfs load-key` for every locked dataset whose keylocation is a file.
That includes a staged copy in flight, whose keylocation is the key file.
Keeping the key file on `/run`, the default, means it's gone at boot, so the staged copy stays locked as it should; an explicit list avoids the attempt altogether.

## Snapshot Tools: sanoid and syncoid

### Exempt the stage from autosnapshots

If sanoid snapshots the pool recursively, it also snapshots the stage.
A snapshot sanoid takes of a staged copy no longer matches the source, so the next catch-up's incremental receive can fail; pruning would also thin out the snapshots the old copies carry.
Give the stage its own section after the pool's, with snapshots and pruning off:

```ini
[tank]
	use_template = production
	recursive = yes

[tank/stage]
	autosnap = no
	autoprune = no
	recursive = yes
```

On NixOS:

```nix
services.sanoid.datasets."tank/stage" = {
  autosnap = false;
  autoprune = false;
  recursive = true;
};
```

This works with `recursive = yes`.
With `recursive = zfs`, sanoid takes one atomic `zfs snapshot -r` and children can't opt out, so keep the stage outside such a tree.

Snapshots sanoid took of the dataset itself are fine: `seed` and the catch-up carry them over, and snapshots taken between the seed and the cutover arrive too.

### syncoid keeps going incrementally

The encrypted dataset carries the same snapshots, with the same GUIDs, as the original.
So after the cutover, syncoid finds the same common snapshot with the backup target and keeps sending incrementally; no new full send is needed.

- syncoid's default sends aren't raw: they need the dataset's key loaded, and the backup target stays as it was (unencrypted, if it was before).
- Raw sends (`--sendoptions=w`) are a different setup that this guide doesn't cover.
- If you replicate a whole pool recursively, leave the stage out with syncoid's `--exclude-datasets` (a regular expression), for example `--exclude-datasets='^tank/stage'`.
