---
icon: lucide/book-open
---

# Guides

All commands here run as root, with the stage set in the environment:

```bash
export ZFS_ENCRYPT_MIGRATE_STAGE=tank/stage
```

On NixOS, take the stage out of boot-time key loading before the first `seed`; see [NixOS boot prompts](#nixos-boot-prompts).

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

If a seed is interrupted or fails, clean up before you run it again.
The tool never destroys anything, so the `@enc-seed` snapshots and any partial staged copy stay behind and make the next `seed` fail until they're gone:

```bash
zfs destroy -r tank/media@enc-seed   # the seed snapshots on the source
zfs destroy -r tank/stage/media      # the partial copy, if there is one: on the stage, never the source
```

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
It sends the same changes without unmounting anything, so you can check the incremental step beforehand.
Anything written after it makes `cutover` refuse and print two `zfs rename -r` commands that move the stale `@enc-final` snapshots aside; run them and rerun `cutover`.
To skip that round trip, run `catchup` after stopping the writers, right before `cutover`.

**4. Bring it back.**
Start the services, re-export the share, and remount it on the NFS clients: they see stale file handles, because the path is now a different dataset.

**5. Check the passphrase.**
`cutover` prints `verify the key with: zfs load-key -n tank/media`.
Run it: it asks for the passphrase and checks it, without changing anything.

```bash
zfs load-key -n tank/media
```

**6. Clean up.**
Destroying the old copy is the point of no return: from then on, the encrypted dataset is the only copy, and the passphrase the only way in.
Once you're satisfied, free the space:

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

If `tree-seed` itself is interrupted or fails, clean up before you run it again, as for `seed`.
This applies only before any `tree-move` ran:

```bash
zfs destroy tank/backups@enc-tree   # the tree snapshot on the source, which tree-seed takes without -r
zfs destroy -r tank/stage/backups   # the partial copy, if there is one: on the stage, never the source
```

Once a child has been moved, `tank/stage/backups` holds it, and its source is gone after step 2, so never destroy `tank/stage/backups` then.
A `tree-seed` rerun part way through, for example after a reboot, refuses with `a migration is already in flight under tank/stage`: that is this tree, so carry on with `tree-move` and `tree-finish` instead.

**2. Move each child, then destroy its source.**
Stop whatever writes to the child first: `tree-move` unmounts it, and the child stays offline until `tree-finish`.

```bash
zfs-encrypt-migrate tree-move tank/backups host1
```

```text
verify the key with: zfs load-key -n -L prompt tank/stage/backups
moved tank/backups/host1; once verified, free it with: zfs destroy -r tank/backups/host1
```

Destroy the source only after `tree-move` printed `moved ...`.
By then it has compared the `@enc-tree` snapshot GUIDs on both sides and checked that the copy is encrypted under `tank/stage/backups`.
A run that was interrupted or failed can leave a partial copy at `tank/stage/backups/host1` instead; running `tree-move` again then refuses and prints the two `zfs destroy -r` commands that remove it and the tree snapshots, after which it starts over.
A rerun for a child that was already moved checks again that nothing was written to the source since, and refuses the same way if something was.

Once you destroy the source, the staged copy is the only copy of that child, and the passphrase the only way into it.
So check the passphrase before every destroy: `zfs load-key -n -L prompt` asks for it instead of reading the key file, and only checks it.
Type it from where you stored it, not from the key file:

```bash
zfs list -r -o name,used tank/stage/backups
zfs load-key -n -L prompt tank/stage/backups   # asks for the passphrase and checks it
zfs destroy -r tank/backups/host1
```

Don't wait long: the unmounted source keeps its `canmount`, so `zfs mount -a` or a reboot mounts it again, and anything written to it then is not in the copy.

Repeat for every child.
Children keep their properties and snapshots, including a `mountpoint=none` or `readonly=on` they had, and volumes move the same way (stop whatever uses a volume first).

**3. Finish the parent.**
Stop whatever writes to `tank/backups`, then:

```bash
zfs-encrypt-migrate tree-finish tank/backups
```

`tree-finish` refuses with `children remain under tank/backups; ...` until every child has been moved and destroyed.
It then sends the parent's last changes and swaps `tank/stage/backups`, with the moved children under it, into place, exactly like `cutover`.
Afterwards, every child has `tank/backups` as its encryption root.

**4. Check the passphrase, then clean up.**

```bash
zfs load-key -n tank/backups
zfs destroy -r tank/stage/old/backups
```

Only the parent has an old copy.
The children you destroyed in step 2 have none, so from step 2 on the encrypted copy of each child is the only one: keep your backups current before you start.

## Datasets Bigger Than the Free Space

`seed` and `cutover` copy a whole dataset with `zfs send`, so the pool needs room for a second copy.
When a single dataset is bigger than the pool's free space, and it has no children to move one by one, `file-move` encrypts it in place instead: it moves the files into a new encrypted dataset in verified batches, and deletes each source file only after its encrypted copy is verified.
The pool needs room for one batch (default `100G`), or the largest file if that is bigger, plus the `--min-free` floor, plus the pool's block-cloned savings (`bclonesaved`), plus lost compression when `compression` is now `off`; not for the whole dataset.
At the end it swaps the new dataset into place under the same name and mountpoint, like `cutover`.

Requirements, on top of the usual ones:

- `rsync` 3.1.3 or newer, with ACL and xattr support, on `PATH`. The Nix package appends one to `PATH` for you; with pip or uv, install it from your distribution.
- Nothing from the list below. Check before you start: `zfs get dedup,reservation,refreservation,origin DATASET`, `zpool get dedup_table_size,dedupratio,checkpoint,bcloneused,bclonesaved,altroot POOL`, and `zfs list -r DATASET` for children.

**Not supported.**
`file-move` relies on every deleted file freeing its space, so it checks for what would stop that before it creates or deletes anything:

- **Deduplication.** A dataset with `dedup` other than `off`, or a pool with deduplicated blocks (a dedup table, `dedup_table_size` above 0; before OpenZFS 2.3, `dedupratio` above 1.00x), even with dedup off now: deleting a deduplicated file frees nothing while another reference to its blocks remains. Refused on the first run.
- **Reservations.** A `reservation` or `refreservation` on the dataset, or a `reservation` on a parent that does not also hold the stage. Refused on every run, with the command that clears it; set it again after the swap.
- **Clones.** A dataset that is a clone: its origin keeps the space of deleted files. Refused on every run; `zfs promote` it first.
- **Pool checkpoints.** A checkpoint keeps deleted space allocated. Refused on every run; `zpool checkpoint -d POOL` first.
- **A pool imported with an altroot.** After `zpool import -R`, `zfs get` adds the altroot to every mountpoint, so the swap would store it in the new dataset's mountpoint. Refused on every run; import the pool without `-R` first.
- **A move whose worst case does not fit.** Before it deletes the first file, `file-move` needs free space above `--min-free` for the largest batch (`--batch-size`, or all the data when that is less; or the largest file, if bigger: moving the others first frees only what their copies take), plus the pool's block-cloned savings `bclonesaved` (a copy of a cloned file takes its full size), plus, with `compression` now `off`, what old compressed data grows by when copied (`logicalreferenced` minus `referenced`). Refused otherwise; checked once per move.
- **Child datasets and volumes.** Refused: for a parent with children, use the [tree commands](#moving-a-big-tree), and `file-move` for a big child on its own; for a volume, `seed` and `cutover`.
- **Per-user, per-group and per-project quotas and project IDs.** A documented limit, not checked: `userquota@`, `groupquota@` and `projectquota@` (and their `objquota@` forms) are not in `zfs get all`, and rsync doesn't copy a file's project ID, so they are not carried over (`seed` and `cutover` keep both). Note them before the move with `zfs userspace`, `zfs groupspace`, `zfs projectspace` and `zfs project -r`, and set them again on the new dataset after the swap.
- **Inode flags.** A documented limit, not checked: rsync doesn't copy `chattr` flags (`+i`, `+a` and the others), so the copies have none. An immutable or append-only file can't be deleted, so the run stops at it until you clear the flag (`chattr -i -a FILE`) and rerun. Note the flags first with `lsattr -R` and set them again after the swap.
- **Quotas on the dataset or the stage.** A documented limit, not checked: the dataset's own `quota` or `refquota` is copied to the encrypted dataset, which holds the directory tree twice (the pristine copy too) besides the files, and one on the stage or a parent of it covers the encrypted dataset as it grows to the full size of the data. Either can stop a run with `Disk quota exceeded`. Raise the one on the encrypted dataset (`zfs set quota=SIZE tank/stage/media`, or `refquota`, and set it back after the swap), or raise or remove the other, then rerun.
- **Data written under other settings.** A documented limit: the copies are written with the current `copies`, `recordsize` and `compression`, so data written with fewer `copies`, with a smaller `recordsize` while `compression` is `off` (each file's last block is padded to a whole record), or under a stronger compression may grow. The admission check counts only what compression saved, and only when `compression` is now `off`; keep spare room above `--min-free` for the rest. Growth beyond that first uses up the `--min-free` floor; then the space check before a batch refuses, with nothing of that batch deleted: free space and rerun.

> [!CAUTION]
> This is the one command that deletes your data. It deletes source files as it goes, so the files end up split between the original dataset and the encrypted copy, and the passphrase becomes the only way into the part that already moved. `file-move` checks that your final key opens the encrypted dataset before it deletes the first file, but store the passphrase safely first, and keep a backup.

**1. Stop and disable everything that uses the dataset, for the whole run.**
A 30 TB dataset takes a day or two, and the dataset is not usable meanwhile: files vanish from it as they move.
Stopping is not enough: a reboot mid-move would start the services again on a half-empty dataset, and some act on what they find missing (Plex's "empty trash", Sonarr and Radarr unmonitoring deleted episodes and movies).

- Stop and disable the services that read or write it (`systemctl disable --now UNIT`), including containers that bind-mount it (turn off their restart policy, for example `docker update --restart=no CONTAINER`, then stop them). The tool refuses a dataset it can't unmount, but a container's bind mount is not detected.
- Stop the NFS server and Samba exports of it.
- Disable snapshot tools (sanoid, zfs-auto-snapshot) for the dataset, then destroy its snapshots. A snapshot keeps deleted files allocated, so no space would be freed; `file-move` refuses while one exists before it changes anything, and checks again before each batch is copied and before it is deleted.

**2. Run it in tmux or screen.**

```bash
tmux new -s file-move
zfs-encrypt-migrate file-move tank/media --delete-source
```

`--delete-source` is required: without it the command refuses before it touches anything.
`--batch-size` (default `100G`, or `ZFS_ENCRYPT_MIGRATE_BATCH_SIZE`) sets how much goes into each batch; a bigger batch means fewer pauses and more free space needed.

Every run first unmounts and remounts the dataset to prove no writer has it open.
The first run then creates the encrypted dataset `tank/stage/media` with the source's properties, copies the directory tree, and checks the final passphrase.
With the default `--final-keylocation prompt` it asks for the passphrase on the terminal, so start the first run from one; the passphrase must be the one in your key file.
A rerun with a different `--final-keylocation` checks the key again, before it deletes anything more.
Each batch then prints a line:

```text
batch 7: 1204 files, 99.8G moved, 4.2T left
```

When no files are left, it swaps the new dataset into place, and prints what to do next.

**3. Stopping and continuing.**
A stop before the swap is safe to continue from: rerun the same command.
Files already copied are not copied again and need no new free space: rsync skips a copy whose size and mtime match the source, and the tool verifies it and deletes the source.
So nothing may write to the dataset between runs either: a file rewritten in place with the same size and mtime would keep its old copy.
After a power loss or `kill -9`, the rerun first removes the partial files rsync left in the encrypted dataset's `.zfs-encrypt-migrate/tmp`.
A failure or Ctrl-C stops rsync, unmounts the encrypted dataset and unloads its key. Once files have moved, the hint tells you the files are split between the two datasets: do not destroy either.
Before the first file is deleted, they are all still in the dataset: when the free-space admission refuses the move, the hint says so and gives the `zfs destroy -r` command that removes the encrypted dataset, in case you give up.
When the error is followed by `file-move aborted.`, the swap had begun: run the commands it prints first, rather than rerunning straight away (see [recovery hints](usage.md#recovery-hints)).

- Ctrl-C, SIGTERM and SIGHUP stop the run cleanly. Use Ctrl-C.
- Ctrl-Z and Ctrl-\ are not caught: they stop only the CLI, while the current rsync batch finishes in the background. Don't use them.
- After a reboot the key file under `/run` is gone. Recreate it with the same passphrase (see [Getting Started](getting-started.md)), then rerun.
- If a batch fails because the pool is too full, free space or lower `--batch-size`, then rerun. Freed space is released asynchronously, so before refusing, the tool runs `zpool sync` and waits up to five minutes for it.

**4. After the swap.**

```bash
zfs load-key -n tank/media    # asks for the passphrase and checks it
```

Then:

- Remount the NFS clients: the new dataset has new file handles. Start the NFS server and Samba again.
- Restart the services that use the dataset.
- Re-enable the snapshot tools. Replication tools such as syncoid restart from a full send, because the new dataset has a new GUID.
- Free the old copy once you have verified the data and the key: `zfs destroy -r tank/stage/old/media`. It holds only empty directories and the `@enc-final` snapshot.

**What the tool checks.**
Before the first file moves, it refuses a dataset that is read-only, not mounted, has a mountpoint of `none` or `legacy`, or has an entry named `.zfs-encrypt-migrate` at its root, and it refuses while another migration is in flight under the stage or another `file-move` runs.
It refuses a key file inside the dataset, symlinks followed, or reached through a bind mount of one of its directories (the file is on the dataset's device), both `--key-file` and the file of `--final-keylocation file://...`: a batch would move it into the dataset that needs it to unlock.
It works only on a path that `/proc/self/mountinfo` lists as the dataset's own mount point, checked before it reads anything there, for the dataset and for the encrypted copy: a mountpoint ending in a space, which `zfs get` strips, would otherwise point it at another directory.
It also refuses everything in the **Not supported** list above that it can detect, since the move would otherwise stall half-way.
Before each batch it checks that the encrypted dataset is still mounted at its private mountpoint, and it accepts only copies that are on that dataset: after an unmount (`zfs umount -a`, say) the mountpoint is a bare directory, and copies there would vanish.
For each batch, rsync copies the files with hard links, ACLs, xattrs and sparseness, and the tool then compares every name against its copy: type, mode, owner and group, size and mtime for regular files, the target for symlinks, and one shared inode for all names of a hard-linked file.
A source file whose ctime changed during its batch is not deleted in that pass, and if a pass deletes nothing, the run stops and asks you to stop the writers.
That is a safety net, not a way to run with writers: a file rewritten in place with the same size and mtime is not copied again, because rsync's quick check takes the earlier copy as current. Stop and disable every writer for the whole run (step 1).
Only the verified names are deleted, after `zpool sync` has made the copies durable.
If a hard-linked file has fewer names in the dataset than its link count says, the run refuses.

**Limits.**

- **No snapshot history.** The new dataset has none of the source's snapshots, and a new GUID, so replication restarts with a full send.
- **An outage for the whole run.** The dataset is unusable from the first batch until the swap.
- **Verification is rsync's transfer checksum plus metadata.** Contents are not read back and compared.
- **Old plaintext blocks stay on disk until overwritten.** Deleting files frees their blocks; it doesn't wipe them.
- **A nearly full pool is slow.** ZFS allocates slowly when little is free; use a smaller `--batch-size`.
- **Tiny files may each grow to one 4K sector.** An unencrypted dataset can store a file of a few bytes inside its block pointer; an encrypted one can't, so the copy takes a whole sector. The space math counts at least 4K per file.
- **The space math is an estimate; `--min-free` is the margin.** It counts at least 4K per file, which can undercount tiny files on pools with bigger sectors (`ashift` above 12), and it leaves out the directory tree, which the staged dataset holds twice until the swap. With many tiny files or directories, raise `--min-free`. If a batch still does not fit, the run waits for freed space (up to five minutes), then stops before copying that batch; a rerun with more free space continues.
- **`sharenfs`, `sharesmb`, `reservation` and `refreservation` are set only after the swap,** so a stopped run never leaves them on the encrypted dataset's private mountpoint.

## Rebuilding a Whole Pool Encrypted

A pool root can't be swapped (`cutover` refuses it), and creating the pool encrypted is the only way to make the root dataset itself encrypted.
The recipe below stages the whole pool `ssd` on another pool, `tank`, recreates `ssd` encrypted and sends the data back.
It's rehearsed step for step in the project's NixOS VM test (`nix/integration-test.nix`).

You need room on `tank` for everything on `ssd`, plus the `--min-free` floor.
The send-back moves the pool root's **children**; anything stored directly in the root dataset of `ssd` isn't part of it, so move such files into a child before step 1.

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
Anything written to `ssd` from here on is lost when the pool is destroyed, so check that nothing was:

```bash
zpool sync ssd
zfs get -rHp -t filesystem,volume -o name,value written@enc-final ssd
```

Every value must be `0`.
If any isn't, stop here and don't destroy the pool: stop whatever still writes, then run `catchup` again (it prints the `zfs rename -r` commands that move the stale `@enc-final` snapshots aside first).

**2. Note the pool root's properties.**

```bash
zfs get -H -s received,local -o property,value all tank/stage/ssd
```

The staged root holds, as received properties, everything that was set on the old pool root, such as `mountpoint`, `compression`, `atime` or `acltype`.
The children inherited those from the root, and after the rebuild they inherit from the new root, so the new root needs them too.
Pass each one as `-O property=value` to `zpool create` in the next step, except `keylocation`: the seed set that on the staged copy itself.

**3. Destroy and recreate the pool encrypted.**

> [!CAUTION]
> This destroys the pool. `zpool destroy` is irreversible: after it, the only copy of the data on `ssd` is `tank/stage/ssd` (and your backups), and the passphrase the only way into it. Check the staged copy, and that the passphrase you stored opens it, before you continue.

```bash
zfs load-key -n -L prompt tank/stage/ssd   # asks for the passphrase and checks it
udevadm settle
zpool destroy ssd
zpool create -O encryption=aes-256-gcm -O keyformat=passphrase \
  -O keylocation=file:///run/zfs-encrypt-migrate.key \
  -O mountpoint=/ssd -O compression=zstd -O atime=off \
  ssd DEVICES...
```

`zfs load-key -n -L prompt` asks for the passphrase instead of reading the key file, and only checks it; type it from where you stored it.
Here `-O mountpoint=/ssd -O compression=zstd -O atime=off` stands for the properties from step 2.
Recreate the pool with your original vdev layout and pool properties too; only the encryption options are new.
`udevadm settle` lets udev finish with any volumes on the pool first.
The recipe uses the staged copy's key file for the new pool root too, so one passphrase covers both.

**4. Send each child back.**

```bash
for d in $(zfs list -H -d1 -t filesystem,volume -o name tank/stage/ssd | tail -n +2); do
  c=${d#tank/stage/}
  zfs send -R -w "$d@enc-final" | zfs recv -u "$c" &&
    zfs load-key -L file:///run/zfs-encrypt-migrate.key "$c" &&
    zfs change-key -i "$c" ||
    { echo "send-back of $d failed; stop and investigate" >&2; break; }
done
```

For each direct child of `tank/stage/ssd`, say `tank/stage/ssd/docker`:

- `zfs send -R -w` sends it raw: the blocks stay encrypted as they are, so nothing is decrypted or re-encrypted and the staged key never needs loading.
- `zfs recv -u ssd/docker` receives it, with all its descendants, snapshots and properties, without mounting. It arrives as its own encryption root, still wrapped by the staged key.
- `zfs load-key -L file://...` loads that key, from the same key file.
- `zfs change-key -i` makes it inherit the key of the pool root `ssd`, so one passphrase unlocks the whole pool.

If any step fails, the loop stops and names the child.
Don't go on to the cleanup: `tank/stage/ssd` is still the only copy of that child and of every child after it.

**5. Switch the pool to its final keylocation and mount.**

```bash
zfs set keylocation=prompt ssd
zfs mount -a
```

**6. Verify.**
Each of these must print nothing.
Every snapshot of every child arrived, with its GUID (bash; the pool root's own snapshots are left out, because the root isn't sent back):

```bash
diff <(zfs list -rH -t snapshot -o name,guid tank/stage/ssd | sed 's|^tank/stage/||' | grep -v '^ssd@' | sort) \
  <(zfs list -rH -t snapshot -o name,guid ssd | grep -v '^ssd@' | sort)
```

Every dataset on the pool has `ssd` as its encryption root:

```bash
zfs get -rH -o name,value encryptionroot ssd | awk '$2 != "ssd"'
```

And your passphrase opens the pool:

```bash
zfs load-key -n ssd
```

**7. Clean up.**

> [!CAUTION]
> This destroys the last copy outside the pool. After it, `ssd` is the only copy of its data (and your backups). Run it only once every check in step 6 passed.

```bash
zfs destroy -r tank/stage/ssd
```

## Unlocking After Reboot

`cutover` sets `keylocation=prompt` on the migrated dataset (unless you passed `--final-keylocation`), so after a reboot it stays locked, and unmounted, until its key is loaded.
The services that use it have to wait for that.
Until the dataset is mounted, its mountpoint is an empty directory on the unencrypted parent: a service that starts first writes there in plaintext, and the dataset later mounts over those files, which hides them but leaves them unencrypted.
So start such services only once the key is loaded and the dataset mounted, by hand or after [zfs-unlock](#with-zfs-unlock) has unlocked it.

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

**Before the first `seed` or `file-move`**, set `boot.zfs.requestEncryptionCredentials` to `false`, or to an explicit list of encryption roots that leaves out the stage, and keep it that way until the stage is empty:

```nix
boot.zfs.requestEncryptionCredentials = false;
# or only the datasets that must unlock at boot:
# boot.zfs.requestEncryptionCredentials = [ "tank/media" ];
```

With the default `true`, importing a pool runs `zfs load-key` for every locked dataset whose keylocation is a file.
That includes a staged copy in flight, whose keylocation is the key file, and after a reboot the key file on `/run` is gone.
The load fails, and the pool import fails with it:

```text
Key load error: Failed to open key material file: No such file or directory
```

`zfs-import.target` is then never reached, and encrypted datasets after the staged copy don't get their keys loaded.
For a pool imported in the initrd, such as the root pool, the same import runs during early boot.

The same default also makes the import ask for every `prompt` key it finds, such as a migrated dataset's, and wait for the answer indefinitely (`boot.zfs.passwordTimeout = 0`).
So if something else, like `zfs-unlock`, loads these keys, keep `requestEncryptionCredentials` at `false`, or at a list of only the datasets that must unlock at boot, after the migration too.

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
- If you replicate a whole pool recursively, leave the stage out with syncoid's `--exclude-datasets` (a regular expression), for example `--exclude-datasets='^tank/stage(/|$)'`, which matches the stage and everything under it but not, say, `tank/stage2`.
