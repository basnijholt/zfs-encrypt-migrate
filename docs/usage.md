---
icon: lucide/terminal
---

# Usage

```bash
zfs-encrypt-migrate [--version] COMMAND [OPTIONS] ARGS
```

Run every command as root, with the host's `zfs` and `zpool` on `PATH`.
Bare `zfs-encrypt-migrate` only shows help.

| Command | What it does |
| --- | --- |
| `seed DATASET` | Copy a dataset tree to an encrypted staged tree, then unload its key. |
| `catchup DATASET` | Send what changed since the seed to the staged tree. |
| `cutover DATASET` | Swap the staged tree into place; the original stays read-only under `STAGE/old`. |
| `tree-seed DATASET` | Copy only the parent to an encrypted staged dataset; its children move one by one. |
| `tree-move DATASET CHILD` | Copy one child subtree under the staged parent; the source stays until you destroy it. |
| `tree-finish DATASET` | Send the parent's last changes and swap the staged tree into place once no children remain. |
| `file-move DATASET --delete-source` | Move a dataset's files in verified batches into an encrypted dataset, deleting each source file once its copy is verified, then swap it into place. |
| `audit [--exact NAME]... [--subtree NAME]... POOL...` | List unencrypted datasets outside the allowlists; exit 1 if there are any. |

## Shared Options

The seven migration, tree and file-move commands take the same options.
`audit` takes none of them.

| Option | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--stage DATASET` | `ZFS_ENCRYPT_MIGRATE_STAGE` | required | Staging dataset, such as `tank/stage`. Never guessed. |
| `--key-file PATH` | `ZFS_ENCRYPT_MIGRATE_KEY_FILE` | `/run/zfs-encrypt-migrate.key` | Key file for the staged tree, as an absolute path; must have mode `600`. |
| `--min-free SIZE` | `ZFS_ENCRYPT_MIGRATE_MIN_FREE` | `1T` | Free space to keep on the stage pool after the copy. |
| `--keyformat FORMAT` | `ZFS_ENCRYPT_MIGRATE_KEYFORMAT` | `passphrase` | Key format of the key file: `passphrase`, `hex` or `raw`. |
| `--final-keylocation LOC` | `ZFS_ENCRYPT_MIGRATE_FINAL_KEYLOCATION` | `prompt` | `keylocation` set on the dataset after the swap. |

Encryption is always `aes-256-gcm`.

**`--stage`.**
The stage holds the staged copy at `<stage>/<leaf>` and the original, after the swap, at `<stage>/old/<leaf>`, where `<leaf>` is the last component of the dataset name.
`seed` and `tree-seed` create `<stage>` and `<stage>/old` with `canmount=off` when they're missing.
`cutover` and the tree commands rename between the dataset and the stage, so both must be on the same pool.
The stage and the dataset may not overlap: every migration and tree command refuses, before any `zfs` command, a dataset that is the stage, contains it or lies inside it.
Because staged names use only the leaf, `tank/a/data` and `tank/b/data` both stage as `tank/stage/data`: migrate one, destroy its old copy, then the other.

**`--key-file`.**
It must be a regular file with mode `600`; a symlink is followed, and its target must be that file.
Only `seed` and `tree-seed` read it: they record it on the staged copy as `keylocation=file://<path>`, and later commands load the key from that keylocation.
Keep the file in place until the swap.
It must be an absolute path; a relative one is a usage error, because `zfs` would reject the relative `file://` keylocation only after `seed` had taken its snapshots.

**`--min-free`.**
Plain bytes, or a number with a `K`, `M`, `G`, `T` or `P` suffix in powers of 1024, optionally followed by `B` or `iB`, in any case: `1099511627776`, `512M`, `1T`, `2GiB`.

**`--final-keylocation`.**
`prompt`, `file:///absolute/path`, or an `http://` or `https://` URL.
It's checked up front, because `zfs` would only reject it at the very end of the swap.

**Dataset names.**
`DATASET`, `--stage` and the audit's names and pools may contain letters, digits and `_ . : -`, separated by `/`, and may not start with `-`.
`CHILD` is a single component without `/`.
Anything else, such as a snapshot name with `@` or a name with spaces, is a usage error before any `zfs` command runs.

## Migration Commands

### seed

```bash
zfs-encrypt-migrate seed tank/data
```

Copies the dataset and everything under it to an encrypted staged tree, while the dataset stays mounted and in use:

1. `zfs snapshot -r DATASET@enc-seed`
2. `zfs send -R DATASET@enc-seed | zfs recv -u -o encryption=aes-256-gcm -o keyformat=FORMAT -o keylocation=file://KEY STAGE/LEAF`
3. `zfs unload-key -r STAGE/LEAF`

`seed` refuses, before it changes anything, with:

| Message | Fix |
| --- | --- |
| `key file PATH not found` | Create the key file. |
| `key file PATH must have mode 600` | `chmod 600 PATH` |
| `DATASET overlaps the stage STAGE; neither may be or contain the other` | Use a stage outside the dataset, and don't migrate the stage or anything under it. |
| `dataset DATASET does not exist` | Check the name. |
| `DATASET is already encrypted` | Nothing to do. |
| `a migration is already in flight under STAGE` | Finish the migration whose staged copy is under the stage, or clean up one whose `seed` or `tree-seed` was interrupted. Once a `tree-move` ran, never destroy the staged parent: it holds the moved children, whose sources are gone. |
| `STAGE/LEAF already exists` | A staged copy with that leaf is in flight. |
| `STAGE/old/LEAF already exists` | Destroy the old copy of an earlier dataset with the same leaf first. |
| `free space on POOL (AVAIL) minus size of DATASET (USED) is below floor MIN_FREE` | Free space, or lower `--min-free`. Sizes are in bytes; `USED` is the dataset's `used`, which includes its children and snapshots. |

`seed` also works on a pool root, with a stage on another pool; `cutover` then refuses, but [rebuilding a pool encrypted](guides.md#rebuilding-a-whole-pool-encrypted) uses exactly that.

### catchup

```bash
zfs-encrypt-migrate catchup tank/data
```

Sends what changed since the seed, without unmounting the dataset:

1. `zfs load-key STAGE/LEAF`
2. `zfs snapshot -r DATASET@enc-final`
3. `zfs send -R -I @enc-seed DATASET@enc-final | zfs recv -u -x encryption STAGE/LEAF`
4. Checks that every dataset has `@enc-final` with the same GUID on both sides, and that every dataset under `STAGE/LEAF` has `STAGE/LEAF` as its `encryptionroot`.
5. `zfs unload-key -r STAGE/LEAF`

`-x encryption` makes a child created after the seed inherit encryption instead of arriving in plaintext.

`catchup` is optional: `cutover` runs the same catch-up itself, unless `@enc-final` already matches on both sides.
Running it separately lets you check the incremental step before the downtime.
If anything is written to the dataset after `catchup`, `cutover` refuses and prints two `zfs rename -r` commands that move both `@enc-final` snapshots aside; run them and rerun `cutover`, which then catches up again.
See [stale snapshots](#stale-snapshots) for why they're renamed rather than destroyed.

| Message | Fix |
| --- | --- |
| `STAGE/LEAF does not exist; run seed first` | Run `seed`. |
| `DATASET@enc-seed does not exist; run seed first` | Run `seed`. |
| `NAME@enc-final already exists; rename it aside with: zfs rename -r NAME@enc-final @enc-final-N` | One line per side, with the same free suffix `N`; run the commands shown, then rerun. |
| `enc-final snapshots of DATASET and STAGE/LEAF differ` | The receive didn't produce matching snapshots; investigate before retrying. |
| `NAME is not encrypted under STAGE/LEAF` | A dataset arrived outside the staged encryption root; investigate before retrying. |

### cutover

```bash
zfs-encrypt-migrate cutover tank/data
```

Swaps the staged tree into the dataset's place.
Stop everything that writes to the dataset first.

1. `zfs load-key STAGE/LEAF`
2. Unmounts the dataset and its descendants, children first, never forcing.
3. Catches up as `catchup` does, unless `@enc-final` already matches on both sides.
4. `zpool sync POOL`, then refuses if `written@enc-final` is non-zero anywhere in the tree.
5. Sets `canmount=noauto` on every filesystem in the tree. Volumes have no `canmount`; they just move with the renames.
6. `zfs rename -u DATASET STAGE/old/LEAF`, then `readonly=on` on it.
7. `zfs rename -u STAGE/LEAF DATASET`
8. Sets `keylocation` to `--final-keylocation`.
9. Mounts, parents first, every filesystem with `canmount=on` whose mountpoint isn't `none` or `legacy`.

On success it prints:

```text
verify the key with: zfs load-key -n DATASET
cutover done; once verified, free the old copy with: zfs destroy -r STAGE/old/LEAF
```

`zfs load-key -n` asks for the passphrase and checks it without changing anything.
Run it before you destroy the old copy: after that, the encrypted dataset is the only copy.

| Message | Fix |
| --- | --- |
| `DATASET is a pool root; cutover is not supported` | See [rebuilding a pool encrypted](guides.md#rebuilding-a-whole-pool-encrypted). |
| `DATASET is on pool POOL but the stage is on OTHER; cutover cannot rename across pools` | Use a stage on the dataset's pool. |
| `STAGE/LEAF does not exist; run seed first` | Run `seed`. |
| `DATASET is a volume; cutover swaps filesystems only, with any volumes under them` | Migrate the volume with its parent filesystem (`cutover` of the parent, or `tree-move`); one directly under the pool root needs [the pool rebuild](guides.md#rebuilding-a-whole-pool-encrypted). |
| `cannot unmount NAME; a writer is probably still running` | Stop the writer and rerun. |
| Any [`catchup`](#catchup) message | As for `catchup`. |
| `written since catchup:` followed by the datasets | Run the two `zfs rename -r ...@enc-final @enc-final-N` commands it prints, then rerun `cutover`. |

## Tree Commands

For a parent with many large children, the tree commands move one child at a time, so you only need room for the parent's own data plus one child.
See the [tree guide](guides.md#moving-a-big-tree) for a walkthrough.

### tree-seed

```bash
zfs-encrypt-migrate tree-seed tank/backups
```

Copies only the parent:

1. `zfs snapshot DATASET@enc-tree` (not recursive)
2. `zfs send -p DATASET@enc-tree | zfs recv -u -o encryption=aes-256-gcm -o keyformat=FORMAT -o keylocation=file://KEY STAGE/LEAF`
3. `zfs unload-key -r STAGE/LEAF`

It refuses with the same messages as `seed`, plus `DATASET is a pool root; the tree move is not supported` and `... tree-seed cannot rename across pools`.
Its free-space check counts only the parent's own data and snapshots (`usedbydataset` plus `usedbysnapshots`).

### tree-move

```bash
zfs-encrypt-migrate tree-move tank/backups host1
```

Copies one direct child, with everything under it, beneath the staged parent:

1. `zfs load-key STAGE/LEAF`
2. Unmounts `DATASET/CHILD` and its descendants, never forcing.
3. `zfs snapshot -r DATASET/CHILD@enc-tree`
4. `zfs send -R DATASET/CHILD@enc-tree | zfs recv -u -x encryption STAGE/LEAF/CHILD`
5. Checks `@enc-tree` GUIDs on both sides, and that everything under `STAGE/LEAF/CHILD` has `STAGE/LEAF` as its `encryptionroot`.
6. `zpool sync POOL`, then refuses if `written@enc-tree` is non-zero anywhere under the child.
7. `zfs unload-key -r STAGE/LEAF`

On success it prints:

```text
verify the key with: zfs load-key -n -L prompt STAGE/LEAF
moved DATASET/CHILD; once verified, free it with: zfs destroy -r DATASET/CHILD
```

Destroy the source child only after a run that printed `moved ...`.
Once it's destroyed, the staged copy is the only one, while its key still comes from the key file.
`-L prompt` makes `zfs load-key -n` ask for the passphrase instead of reading that file, so it checks the passphrase you stored; run it before every destroy.

The source child stays where it is, unmounted, until you destroy it.
It keeps its `canmount`, so `zfs mount -a` or a reboot mounts it again; destroy it soon after you've checked the copy.

When the child is already staged, a rerun of `tree-move` checks the staged copy instead of copying:

- If `DATASET/CHILD` has no `@enc-tree`, it was created after an earlier move whose source was destroyed, so the staged child may be the only copy of the original data. It refuses without any cleanup commands; resolve it by hand, for example by renaming the new `DATASET/CHILD` aside.
- If any dataset under `DATASET/CHILD` lacks an `@enc-tree` with the same GUID under `STAGE/LEAF/CHILD`, or anything there isn't encrypted under `STAGE/LEAF`, the staged copy is partial, left by a run that was interrupted or failed. It refuses with the commands that remove it.
- Otherwise it checks the source as a move does: it unmounts it, runs `zpool sync` and refuses if `written@enc-tree` is non-zero anywhere under it, with the same two commands. Only if nothing was written was the child already moved: it prints `DATASET/CHILD was already moved to STAGE/LEAF/CHILD`, then the two lines above, and exits 0.

| Message | Fix |
| --- | --- |
| `DATASET is on pool POOL but the stage is on OTHER; tree-move cannot rename across pools` | Use a stage on the dataset's pool. |
| `STAGE/LEAF@enc-tree does not exist; run tree-seed first` | Run `tree-seed`. |
| `dataset DATASET/CHILD does not exist` | Check the name. |
| `STAGE/LEAF/CHILD already exists but DATASET/CHILD has no @enc-tree; DATASET/CHILD looks recreated` | Keep `STAGE/LEAF/CHILD`: it may be the only copy of the original data. Resolve it by hand, for example by renaming the new `DATASET/CHILD` out of `DATASET` with `zfs rename`. |
| `STAGE/LEAF/CHILD already exists but is not a complete copy of DATASET/CHILD; a tree-move was interrupted or failed` | Only a run that printed `moved ...` finished the child; don't destroy the source. Run the two commands it prints, `zfs destroy -r DATASET/CHILD@enc-tree` and `zfs destroy -r STAGE/LEAF/CHILD`, then rerun. |
| `free space on POOL (AVAIL) minus size of DATASET/CHILD (USED) is below floor MIN_FREE` | Free space, or lower `--min-free`. |
| `cannot unmount NAME; a writer is probably still running` | Stop the writer and rerun. |
| `enc-tree snapshots of DATASET/CHILD and STAGE/LEAF/CHILD differ`, or `NAME is not encrypted under STAGE/LEAF` | The received copy doesn't match; investigate, then run the two `zfs destroy -r` commands it prints and rerun. |
| `written since the tree snapshot:` followed by the datasets | Stop the writer, run the two `zfs destroy -r` commands it prints (the stale snapshot and the copy without those writes), then rerun. |

### tree-finish

```bash
zfs-encrypt-migrate tree-finish tank/backups
```

Once every child has been moved and destroyed, sends the parent's last changes and swaps the staged tree, with the moved children, into place:

1. `zfs load-key STAGE/LEAF`
2. Unmounts the parent, never forcing.
3. `zfs snapshot DATASET@enc-tree-final` (not recursive)
4. `zfs send -p -I @enc-tree DATASET@enc-tree-final | zfs recv -u STAGE/LEAF`
5. Checks `@enc-tree-final` GUIDs on both sides.
6. `zpool sync POOL`, then refuses if `written@enc-tree-final` is non-zero.
7. Swaps exactly as `cutover` does.

On success it prints:

```text
verify the key with: zfs load-key -n DATASET
tree-finish done; once verified, free the old copy with: zfs destroy -r STAGE/old/LEAF
```

| Message | Fix |
| --- | --- |
| `DATASET is a pool root; the tree move is not supported` | See [rebuilding a pool encrypted](guides.md#rebuilding-a-whole-pool-encrypted). |
| `DATASET is on pool POOL but the stage is on OTHER; tree-finish cannot rename across pools` | Use a stage on the dataset's pool. |
| `STAGE/LEAF@enc-tree does not exist; run tree-seed first` | Run `tree-seed`. |
| `DATASET@enc-tree does not exist; run tree-seed first` | Run `tree-seed`. |
| `children remain under DATASET; move each with tree-move and destroy it first` | Finish moving the children. |
| `NAME@enc-tree-final already exists; rename it aside with: zfs rename NAME@enc-tree-final @enc-tree-final-N` | One line per side, with the same free suffix `N`; run the commands shown, then rerun. |
| `cannot unmount NAME; a writer is probably still running` | Stop the writer and rerun. |
| `enc-tree-final snapshots of DATASET and STAGE/LEAF differ` | The received copy doesn't match; investigate before retrying. |
| `written since the final snapshot:` followed by the dataset | Run the two `zfs rename ...@enc-tree-final @enc-tree-final-N` commands it prints, then rerun. |

## File-Move Commands

For one dataset that is bigger than its pool's free space, `file-move` moves the files instead of sending the dataset.
See the [file-move guide](guides.md#datasets-bigger-than-the-free-space) for a walkthrough and its limits.
It needs `rsync` 3.1.3 or newer, with ACL and xattr support, on `PATH`.

### file-move

```bash
zfs-encrypt-migrate file-move tank/media --delete-source
```

Takes the [shared options](#shared-options) and:

| Option | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--batch-size SIZE` | `ZFS_ENCRYPT_MIGRATE_BATCH_SIZE` | `100G` | Target bytes per batch, in the `--min-free` size syntax. A hard-linked file group bigger than this forms a batch of its own. |
| `--delete-source` | none | off | Required. Confirms that each source file is deleted once its encrypted copy is verified. |

One run does the whole job, and a rerun after a stop before the swap continues where it left off.
Stop everything that uses the dataset first, and keep it stopped until the swap.
Without `--delete-source` it refuses before it touches anything: `file-move deletes each source file once its encrypted copy is verified; pass --delete-source to confirm`.

It runs, in this order:

1. Takes an exclusive lock on `/run/zfs-encrypt-migrate.lock`, so two `file-move` runs never overlap.
2. Refuses, changing nothing, unless the key file is valid and the dataset is a mounted leaf filesystem, not a pool root, volume or already encrypted dataset, writable, with a real mountpoint, on the stage's pool and outside the stage, with nothing named `.zfs-encrypt-migrate` at its root, and nothing else in flight under the stage. It also refuses whatever would keep deleted files from freeing their space: a `reservation` or `refreservation` on the dataset, a `reservation` on a parent that does not also hold the stage, a dataset that is a clone, a checkpoint on the pool, or, while files are left to move, a snapshot of the dataset. On a pool with block-cloned data it also needs free space above `--min-free` for the pool's `bclonesaved` plus one batch: a copy of a cloned file takes its full size, while deleting one reference to it frees nothing.
3. Unmounts the dataset, which fails while a writer has it open, and mounts it again with `-o noatime`, so the tool's own reads never write access times.
4. On the first run, creates `STAGE/LEAF` with `aes-256-gcm`, `canmount=noauto`, a private mountpoint under `/run/zfs-encrypt-migrate/` and the source's properties (`mountpoint`, `canmount`, `sharenfs`, `sharesmb`, `reservation` and `refreservation` wait until the swap). Every run loads its key and mounts it. It refuses unless the dataset is then the filesystem mounted at that mountpoint.
5. Copies the directory tree, twice: into the staged root, and into a pristine copy of the original directory attributes under `.zfs-encrypt-migrate/dirs` there.
6. Before the first deletion, checks the final key: with `--final-keylocation prompt` it needs a terminal and runs `zfs load-key -n -L prompt STAGE/LEAF`; with another location it runs `zfs load-key -n -L LOCATION STAGE/LEAF`. The staged dataset records which location passed, so a rerun with another `--final-keylocation` checks again.
7. Removes the partial files that an rsync stopped by a power loss or SIGKILL left in `.zfs-encrypt-migrate/tmp` of the staged dataset (nothing else there). Then repeats until no files remain. Scans the dataset, refuses if it has a snapshot, groups the names by inode, and cuts batches of at most `--batch-size`. For each batch: refuses unless the staged dataset is still the filesystem mounted at its mountpoint (after an unmount that path is a bare directory, and copies there would vanish), waits until the files without a copy yet fit above `--min-free` (`zpool sync`, up to 30 tries, 10 seconds apart; copies an earlier stopped run left need no new space), copies with `rsync -aHAXS --numeric-ids --modify-window=-1` (times compared to the nanosecond, so a copy that is off by less than a second is copied again), verifies every name against its copy (which must be on the staged dataset), runs `zpool sync`, refuses if a snapshot appeared meanwhile, and only then deletes the verified source names. It never uses `rsync --delete` or `--remove-source-files`.
8. When only directories are left: unmounts the dataset again (refusing while it is busy), mounts it with `-o noatime` and runs `zpool sync`, so the last deletes are settled; then `zfs snapshot DATASET@enc-final`, rescan, rsync the directory attributes (restoring the original directory mtimes), unmount the dataset, `zpool sync`, and refuse if `written@enc-final` is non-zero.
9. Removes `.zfs-encrypt-migrate` from the staged dataset and unmounts it, then swaps as `cutover` does (rename the original to `STAGE/old/LEAF`, then the staged dataset into place), and sets the source's `mountpoint`, `sharenfs`, `sharesmb`, `reservation`, `refreservation` and `canmount` on the swapped dataset, removes its marker properties and mounts it.

Each batch prints one line:

```text
batch 7: 1204 files, 99.8G moved, 4.2T left
```

On success it prints:

```text
verify the key with: zfs load-key -n DATASET
remount NFS clients: the new dataset has new file handles
re-enable snapshot tools for DATASET; replication restarts with a full send
file-move done; once verified, free the old copy with: zfs destroy -r STAGE/old/LEAF
```

The verification of a name compares type, mode, owner and group; for regular files size and mtime; for symlinks the target; for device nodes `st_rdev`; and requires all copies of a hard-linked file to share one inode.
A source file whose `ctime` changed since the scan is skipped for that pass, and a destination mismatch fails the run with nothing of the batch deleted.

Not carried over: per-user, per-group and per-project quotas (`userquota@`, `groupquota@`, `projectquota@` and their `objquota@` forms) and the project IDs of files. `zfs get all` does not list those quotas and rsync does not copy project IDs; `seed` and `cutover` keep both, since they send the dataset. Note them before the move (`zfs userspace`, `zfs groupspace`, `zfs projectspace`, `zfs project -r`) and set them again on the swapped dataset.

On a failure or interrupt, rsync is stopped, the staged dataset is unmounted and its key unloaded, and before the swap the hint is `file-move stopped; the files are split between DATASET and STAGE/LEAF; do not destroy either.` followed by `Rerun the same command to continue.`
From the swap on, the [recovery hints](#recovery-hints) of `cutover` apply, followed by the commands that finish the property fix-ups; follow them rather than rerunning.
A stop between unmounting the dataset and mounting it again in step 3 or step 8 adds `remount it with: zfs mount DATASET`.
From the final unmount of step 8 on, the hints remount with `zfs mount -a` instead.

| Message | Fix |
| --- | --- |
| `another file-move is running (lock PATH)` | Wait for it, or check for a stuck process. |
| `DATASET is a pool root; file-move is not supported` | Not possible; see [rebuilding a pool encrypted](guides.md#rebuilding-a-whole-pool-encrypted). |
| `DATASET has child datasets; use the tree commands, or file-move each child` | Use the [tree commands](#tree-commands), or `file-move` a big child on its own. |
| `DATASET is a volume; file-move moves the files of a filesystem` | Use `seed` and `cutover`. |
| `DATASET is already encrypted` | Nothing to do, unless DATASET still carries `zfs-encrypt-migrate:*` properties (`zfs get all DATASET`): then a stop between the swap and its final steps left them. Run the recovery commands printed then: they inherit the markers, set `mountpoint` and `canmount`, and mount it. |
| `DATASET is read-only; file-move must delete its files` | `zfs set readonly=off DATASET`, then rerun. |
| `DATASET has mountpoint none; file-move needs a mounted path` (or `legacy`) | Give it a real mountpoint. |
| `DATASET is not mounted` | `zfs mount DATASET`, then rerun. |
| `PATH exists; file-move needs that name for its own files` | Rename that entry in the dataset. |
| `DATASET has reservation=BYTES, which keeps deleted space reserved; clear it with: zfs set reservation=none DATASET (set it again on the new dataset after the move)` (or `refreservation`) | Run the printed `zfs set`, then rerun; set the reservation on the new dataset after the swap. |
| `PARENT has reservation=BYTES, which keeps space freed in DATASET reserved; clear it with: zfs set reservation=none PARENT (set it again after the move)` | Run the printed `zfs set`, then rerun; set the reservation again after the swap. Or use a stage inside PARENT. |
| `DATASET is a clone of ORIGIN; its origin keeps the space of deleted files; promote it first: zfs promote DATASET` | `zfs promote DATASET`, then rerun. The promote moves the origin's snapshots to DATASET, so the snapshot refusal follows until they are destroyed, which needs the former origin dataset gone first. |
| `pool POOL has a checkpoint, which keeps deleted space allocated; discard it first: zpool checkpoint -d POOL` | `zpool checkpoint -d POOL`, then rerun. |
| `pool POOL has SIZE of block-cloned data; copies are not clones, so file-move may need that much more space: free space above the floor (SIZE) must cover it plus one batch (SIZE)` | Free space, lower `--batch-size`, or remove clones you no longer need (`zpool get bcloneused,bclonesaved POOL` shows how much is cloned). |
| `STAGE/LEAF exists and is not a file-move of DATASET` | Another migration owns that name; finish or clear it first. |
| `a migration is already in flight under STAGE` | Finish the other migration first. |
| `STAGE/old/LEAF already exists` | Destroy or rename the old copy of an earlier migration. |
| `cannot unmount DATASET; a writer is probably still running` | Stop the writer and rerun. |
| `run file-move once from a terminal so it can check the passphrase` | Run it from a terminal (tmux), or pass `--final-keylocation file:///absolute/path`. |
| `the key for STAGE/LEAF did not load from LOCATION; nothing was deleted` | Fix the key or location; the first mistyped passphrase deletes nothing. |
| `DATASET has snapshots, which keep deleted files allocated: ...` | Disable snapshot tools for the dataset, then destroy the snapshots it lists (`zfs destroy SNAPSHOT`). |
| `PATH has N hard links but only M were found` | The dataset changed during the scan: a writer added or removed a link. Stop the writers and rerun. |
| `not enough free space on POOL for the next batch (SIZE) above the floor SIZE` | Free space or lower `--batch-size`, then rerun. |
| `STAGE/LEAF is no longer mounted at PATH; nothing of this batch was deleted` | Something unmounted the staged dataset or mounted over it during the run (for example `zfs umount -a`). Rerun: it mounts the dataset again and continues. |
| `STAGE/LEAF is not mounted at PATH` | `zfs mount` succeeded but nothing is mounted at the staged dataset's mountpoint; check `zfs get mountpoint,mounted STAGE/LEAF` and `findmnt PATH`. |
| `the copy of NAME is not on the staged dataset` | The copy landed outside the staged dataset; nothing of the batch was deleted. Rerun. |
| `N files kept changing during the move; stop the writers and rerun` | Stop whatever writes to the dataset. |
| `N files appeared in DATASET during the swap; stop the writers and rerun` | Stop the writers and rerun. |
| `DATASET was written after the final scan` | Rename `@enc-final` aside with the commands it prints, then rerun. |
| `STAGE/LEAF is mounted at PATH, which overlaps DATASET at PATH` | Change the staged mountpoint so it does not contain or lie inside the dataset's. |

## Audit

```bash
zfs-encrypt-migrate audit --exact tank --subtree tank/stage tank
```

Lists every filesystem and volume of the named pools whose `encryption` is `off`, except the allowlisted ones, one per line on stdout.

- `--exact NAME` allows exactly that dataset.
- `--subtree NAME` allows that dataset and its descendants, by path component: `--subtree tank/media` covers `tank/media/photos` but not `tank/media2`.
- Both are repeatable.

It exits 1 if it lists anything.
It fails closed: if `zfs list` fails for any pool, for example one that isn't imported, it prints the error and exits 1 rather than reporting a clean result.
Naming no pool is a usage error.

A typical allowlist is the pool root (`--exact tank`), unless you created the pool encrypted, and the stage (`--subtree tank/stage`), whose old copies are unencrypted on purpose.

## Output and Exit Codes

Errors go to stderr as `error: <message>`, followed by any recovery hints.
Success lines go to stdout.
When a `zfs` or `zpool` command fails, the message is the command followed by `failed:` and its error output; this includes `zfs` missing from `PATH` or not running as root.

| Exit status | Meaning |
| --- | --- |
| `0` | Success; for `audit`, nothing unencrypted outside the allowlists. |
| `1` | A refusal or a failed `zfs` command; for `audit`, an unencrypted dataset or a pool it couldn't list. |
| `2` | Usage error: a missing `--stage`, an invalid name, size or keylocation, or no pool for `audit`. |
| `130` | A migration, tree or file-move command interrupted by Ctrl-C, SIGTERM or SIGHUP. `audit` has nothing to clean up: Ctrl-C ends it with 1, and SIGTERM or SIGHUP end it like any other process. |

## Recovery Hints

If a command fails or is interrupted after it loaded the staged key, the key is unloaded again before it exits.
If that unload fails too, it prints:

```text
could not unload key; run: zfs unload-key -r STAGE/LEAF
```

Once `cutover`, `tree-move` or `tree-finish` has started on the source dataset, the error is followed by `<command> aborted.` and the commands for the step it reached.
`DATASET` is the dataset being swapped, or `DATASET/CHILD` for `tree-move`, which never gets past the first step:

| Step reached | Hint |
| --- | --- |
| Before the swap | `DATASET may be unmounted; remount with: zfs mount -a` |
| `canmount` set to `noauto` | `DATASET is unmounted and canmount may be noauto; restore with:`, a `zfs set canmount=on NAME` line for each dataset that had it, and `zfs mount -a` |
| Original renamed aside | `the original is at STAGE/old/LEAF (canmount=noauto, readonly=on); put it back with:` `zfs rename -u STAGE/old/LEAF DATASET`, `zfs inherit readonly DATASET`, the `canmount=on` lines, and `zfs mount -a` |
| Staged copy renamed into place | `the swap completed; only the final steps failed. Finish with:` `zfs set keylocation=LOC DATASET` and `zfs mount -a` |

Once `tree-move` has unmounted the source and its `@enc-tree` snapshots exist, a failure or interrupt also prints `STAGE/LEAF/CHILD may hold a partial copy; remove it and the tree snapshots, then rerun tree-move:`, followed by `zfs destroy -r DATASET/CHILD@enc-tree` and `zfs destroy -r STAGE/LEAF/CHILD`.
Until both have run, a rerun refuses.

### Stale snapshots

When a retry is blocked by an `@enc-final` or `@enc-tree-final` left over from an earlier attempt, the tool prints commands that rename it aside, on both sides, to the first `-N` suffix that neither side uses yet:

```text
zfs rename -r tank/data@enc-final @enc-final-1
zfs rename -r tank/stage/data@enc-final @enc-final-1
```

Renaming, not destroying, is what makes the retry work.
The staged copy still matches its newest snapshot, so the next incremental receive can build on it; destroy that snapshot and `zfs recv` refuses the staged copy as modified, which leaves only a fresh `seed`.
A renamed snapshot keeps its GUID, so the retry's receive skips it instead of sending it again.
`@enc-tree-final` is renamed without `-r`, because `tree-finish` snapshots only the parent.
Once the migration is done, the renamed snapshots are ordinary snapshots you can destroy.

### Interrupts and hangups

Ctrl-C (SIGINT), SIGTERM and SIGHUP all take the same path: a running `zfs send` and `zfs recv` are stopped, the staged key is unloaded, `error: interrupted` and the hints are printed, and the exit status is 130.
While it unloads the key, further signals are ignored, so a second Ctrl-C can't skip the unload.

A signal that was already ignored when the tool started stays ignored.
For a long seed over SSH, `nohup zfs-encrypt-migrate seed ...` therefore survives a dropped connection instead of aborting.

## Safety Model

- **It never runs `zfs destroy`.** Old copies and moved children are left for you, with the exact command printed; stale snapshots get a printed rename instead. The one thing it does delete is, with `file-move --delete-source`, a source file whose encrypted copy is verified.
- **Staged keys stay unloaded.** The staged copy keeps every mountpoint set locally in the original tree, so its key is loaded only while the tool works on it. `seed`, `catchup`, `tree-seed` and `tree-move` unload it when they finish, and any failure or interrupt part way through unloads it too.
- **One migration in flight.** `seed` and `tree-seed` refuse while anything other than `<stage>`, `<stage>/old` and the old copies under it exists under the stage.
- **A free-space floor.** `seed`, `tree-seed` and `tree-move` refuse unless the stage pool's free space minus the size of what they copy stays at least `--min-free`.
- **No lost writes.** `cutover`, `tree-move` and `tree-finish` unmount the source first and refuse if it's busy. After the final snapshot they run `zpool sync` and refuse if `written@<snapshot>` is non-zero anywhere in the tree.
- **Nothing lands unencrypted.** Catch-up and `tree-move` receives use `-x encryption`. After each catch-up and each `tree-move`, every dataset in the received tree is checked to have the staged copy as its `encryptionroot`, and snapshots are compared by GUID.
- **`file-move` deletes only verified files, only with consent.** Each source file is unlinked only after its copy matches in type, mode, owner, size, mtime and link structure and the pool has synced, and only with `--delete-source`. It refuses while the dataset has snapshots, and checks your final key before the first deletion.
- **Recovery hints that match the failure.** See [recovery hints](#recovery-hints).
- **Signals are cleanups, not crashes.** See [interrupts and hangups](#interrupts-and-hangups).

And its limits:

- **Old plaintext blocks stay on disk until they're overwritten.** Destroying the old copy frees its blocks; it doesn't wipe them.
- **You need free space for one dataset at a time** (or the parent plus one child), until you destroy the old copy. `file-move` needs only one batch plus the floor.
- **Plan for writer downtime during the cutover.** The dataset is unmounted for the final catch-up and the swap.
- **NFS clients see stale file handles after a swap.** The path is now served by a different dataset; remount it on the clients.
- **Children moved with `tree-move` get no old copy to fall back on.** Once you destroy a moved source child, the encrypted copy is the only one; only the parent lands in `<stage>/old`. Check the passphrase you stored first, with the `zfs load-key -n -L prompt STAGE/LEAF` line that `tree-move` prints.
- **A dataset whose last component is `old` can't be migrated.** Its staged copy would be `<stage>/old`, where the old copies live.
- **On NixOS, keep the stage out of boot-time key loading.** With the default `boot.zfs.requestEncryptionCredentials = true`, a reboot while a staged copy exists makes the pool import fail with `Key load error`. Before the first `seed`, set it to `false` or to a list without the stage, and keep it that way until the stage is empty; see [NixOS boot prompts](guides.md#nixos-boot-prompts).
- **Volumes move with their parent filesystem.** `cutover` refuses a volume on its own (a volume directly under the pool root needs the pool rebuild), and since a volume can't be unmounted, the tool can't tell that it's in use: stop whatever uses it first.
- **It needs `zfs rename -u`.** It is tested on OpenZFS 2.4.

## NixOS Module Options

```nix
services.zfs-encrypt-migrate = {
  enable = true;
  audit = {
    enable = true;
    pools = [ "tank" ];
    exact = [ "tank" ];
    subtrees = [ "tank/stage" ];
  };
};
```

| Option | Default | Description |
| --- | --- | --- |
| `enable` | `false` | Install the CLI into the system profile. |
| `package` | flake package | Package providing `zfs-encrypt-migrate`. Set by `nixosModules.default`; set it yourself when importing `nix/nixos-module.nix` directly. |
| `audit.enable` | `false` | Run the audit on a timer. Works without `enable`. |
| `audit.pools` | `[ ]` | Pools to audit. Must be non-empty when the audit is enabled. |
| `audit.exact` | `[ ]` | Datasets allowed to stay unencrypted, matched by whole name (`--exact`). |
| `audit.subtrees` | `[ ]` | Datasets allowed to stay unencrypted together with their descendants (`--subtree`). |
| `audit.onCalendar` | `"daily"` | systemd calendar expression for the timer. |
| `audit.onFailure` | `[ ]` | Units to start when the audit fails. |

The audit runs as the oneshot `zfs-encrypt-migrate-audit.service`, ordered after `zfs-import.target`, from the `Persistent` `zfs-encrypt-migrate-audit.timer`.
It uses the host's `boot.zfs.package`, runs as root (it needs `/dev/zfs`) and is otherwise sandboxed, for example with `ProtectSystem=strict` and `ProtectHome=true`.
