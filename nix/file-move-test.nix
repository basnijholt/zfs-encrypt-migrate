# End-to-end test of file-move against *real* OpenZFS and *real* rsync.
#
# The unit suite runs rsync for real but simulates zfs, so it cannot show how
# the kernel frees space, mounts, counts written@ or prompts for a passphrase.
# This test boots a VM with one small pool and moves a dataset whose data
# exceeds the pool's free space, interrupts a move and reruns it, and checks
# the passphrase at a terminal.
{
  pkgs,
  module,
  package,
}:
let
  # Ctrl-C in the middle of a file batch: calls 1 and 2 copy the directory
  # tree, so call 4 is the second batch. Only the CLI gets the signal, and
  # that batch copies slowly, so the CLI stops rsync part-way through a file.
  fakeRsync = pkgs.writeShellScriptBin "rsync" ''
    calls=$(( $(cat /tmp/rsync-calls 2>/dev/null || echo 0) + 1 ))
    echo "$calls" > /tmp/rsync-calls
    if [ "$calls" -eq 4 ]; then
      kill -INT "$PPID"
      exec ${pkgs.rsync}/bin/rsync --bwlimit=1M "$@"
    fi
    exec ${pkgs.rsync}/bin/rsync "$@"
  '';
  # Runs $2 at a terminal (script) and types $1 at each passphrase prompt until it exits.
  # zfs flushes pending terminal input when it turns echo off, and asks up to three times
  # on a wrong passphrase, so each line goes in only once its own prompt is waiting.
  atTerminal = pkgs.writeShellScript "at-terminal" ''
    set -u
    dir=$(mktemp -d)
    mkfifo "$dir/in"
    # Held open, so script never sees end of input.
    exec 3<>"$dir/in"
    script -qec "$2" /dev/null <"$dir/in" >"$dir/out" 2>&1 &
    pid=$!
    typed=0
    while kill -0 "$pid" 2>/dev/null; do
      prompts=$(grep -o 'Enter passphrase' "$dir/out" | wc -l)
      prompter=$(pgrep -n -f 'zfs load-key -n -L prompt')
      if [ "$prompts" -gt "$typed" ] && [ -n "$prompter" ] && stty -a -F "/proc/$prompter/fd/0" 2>/dev/null | grep -qw -- -echo; then
        printf '%s\n' "$1" >&3
        typed=$((typed + 1))
      fi
      sleep 0.1
    done
    wait "$pid"
    status=$?
    cat "$dir/out"
    exit "$status"
  '';
in
pkgs.testers.runNixOSTest {
  name = "zfs-encrypt-migrate-file-move";

  nodes.machine =
    { ... }:
    {
      imports = [ module ];

      # Real ZFS in the guest; its zfs/zpool land on the system PATH.
      boot.supportedFilesystems = [ "zfs" ];
      # ZFS refuses to import without a hostId; the value is arbitrary.
      networking.hostId = "3e1647d3";

      environment.systemPackages = [
        pkgs.acl
        pkgs.attr
        pkgs.rsync
        pkgs.util-linux
      ];

      services.zfs-encrypt-migrate = {
        enable = true;
        inherit package;
      };

      # Backing disk for the tank pool (/dev/vdb), small enough that the moved
      # dataset is bigger than the pool's free space.
      virtualisation.emptyDiskImages = [ 1024 ];
      virtualisation.memorySize = 2048;
    };

  testScript = ''
    import re

    machine.wait_for_unit("multi-user.target")
    machine.succeed("modprobe zfs")
    # The CLI uses the guest's zfs and rsync, so record which ones these assertions ran against.
    print(machine.succeed("zfs version"))
    print(machine.succeed("rsync --version | head -n 1"))
    # The package appends its own rsync to PATH, after any rsync already there.
    assert "${pkgs.rsync}/bin" in machine.succeed("cat \"$(readlink -f \"$(command -v zfs-encrypt-migrate)\")\"")
    machine.succeed("zpool create -O mountpoint=/tank tank /dev/vdb")
    machine.succeed(
        "printf test-passphrase-123 > /run/zfs-encrypt-migrate.key && chmod 600 /run/zfs-encrypt-migrate.key"
        " && cp -p /run/zfs-encrypt-migrate.key /run/final.key"
    )

    CLI = "zfs-encrypt-migrate file-move --stage tank/stage --min-free 32M"
    FINAL = "--final-keylocation file:///run/final.key"
    get = lambda p, d: machine.succeed(f"zfs get -H -p -o value {p} {d}").strip()
    files = lambda: machine.succeed("find /srv/media ! -type d | sort")


    def populate(episode, movie, big):
        """A media tree with the shapes file-move must keep: hard links between downloads/ and tv/show/,
        a symlink, a fifo, odd names, xattrs, ACLs, owners, modes and directory times."""
        machine.succeed(
            "cd /srv/media && mkdir -p downloads tv/show movies big empty/nested"
            f" && for i in 1 2 3 4 5 6; do head -c {episode} /dev/urandom > downloads/ep$i.mkv; done"
            " && for i in 1 2 3 4; do ln downloads/ep$i.mkv tv/show/S01E0$i.mkv; done"
            f" && for i in $(seq -w 1 20); do head -c {movie} /dev/urandom > movies/m$i.bin; done"
            # Bigger than a batch, so it moves in a batch of its own.
            f" && head -c {big} /dev/urandom > big/huge.bin"
            r" && printf a > 'downloads/with space.txt' && printf b > $'downloads/new\nline.txt'"
            r" && printf c > downloads/-dash.txt && printf d > $'downloads/bad\xffname.txt'"
            " && ln -s ../../downloads/ep1.mkv tv/show/latest.mkv && mkfifo downloads/pipe"
            " && setfattr -n user.comment -v hello downloads/ep2.mkv && setfattr -n user.shelf -v tv tv"
            " && setfacl -m u:1234:rw movies/m01.bin"
            # A default ACL added after the files exist: only the file created after it inherits it.
            " && setfacl -d -m g:5678:rx tv/show && printf e > tv/show/notes.txt"
            " && chown 1234:5678 movies/m02.bin downloads/-dash.txt && chown -h 1234:5678 tv/show/latest.mkv"
            " && chmod 640 movies/m03.bin && chmod 2750 big"
            # Last, since creating entries changes a directory's mtime.
            " && touch -d '2001-02-03 04:05:06.123456789' downloads tv/show"
            " && touch -d 2010-01-01 tv movies big empty empty/nested ."
        )


    def manifest(name):
        """Record /srv/media in /tmp/<name>.*; return its xattrs and ACLs, one sorted entry per file."""
        machine.succeed(
            f"cd /srv/media && find . -printf '%y %m %U %G %s %T@ %p -> %l\\n' | sort > /tmp/{name}.find"
            f" && find . -type f -print0 | sort -z | xargs -0 sha256sum > /tmp/{name}.sha"
        )
        a, b = machine.succeed("cd /srv/media && stat -c %i downloads/ep1.mkv tv/show/S01E01.mkv").split()
        assert a == b, f"{name}: hard link broken ({a} != {b})"
        # Both walk in readdir order, which differs between two directories with the same names.
        blocks = lambda out: sorted(block for block in out.split("\n\n") if block.strip())
        return (
            blocks(machine.succeed("cd /srv/media && getfattr -R -d -m - .")),
            blocks(machine.succeed("cd /srv/media && getfacl -R .")),
        )


    def same(before, after):
        machine.succeed(f"diff -u /tmp/{before}.find /tmp/{after}.find && diff -u /tmp/{before}.sha /tmp/{after}.sha")


    def staged_closed():
        assert get("mounted", "tank/stage/media") == "no"
        assert get("keystatus", "tank/stage/media") == "unavailable"


    with subtest("file-move refuses without --delete-source, with snapshots, with children and while busy"):
        machine.succeed("zfs create -o mountpoint=/srv/media tank/media && mkdir /srv/media/d && echo a > /srv/media/d/a")
        before = files()
        out = machine.fail(f"{CLI} {FINAL} tank/media 2>&1")
        assert (
            "file-move deletes each source file once its encrypted copy is verified; pass --delete-source to confirm"
            in out
        ), out
        machine.succeed("zfs create tank/media/kid")
        out = machine.fail(f"{CLI} {FINAL} --delete-source tank/media 2>&1")
        assert "tank/media has child datasets" in out, out
        machine.succeed("zfs destroy tank/media/kid")
        # Type=exec: systemd-run returns once sleep runs inside /srv/media.
        machine.succeed('systemd-run --unit=busy -p Type=exec --working-directory=/srv/media "$(command -v sleep)" 600')
        out = machine.fail(f"{CLI} {FINAL} --delete-source tank/media 2>&1")
        assert "cannot unmount tank/media; a writer is probably still running" in out, out
        machine.succeed("systemctl stop busy")
        # Nothing was created by any refusal so far.
        machine.fail("zfs list tank/stage")
        machine.succeed("zfs snapshot tank/media@autosnap_1")
        out = machine.fail(f"{CLI} {FINAL} --delete-source tank/media 2>&1")
        assert "tank/media has snapshots" in out and "tank/media@autosnap_1" in out, out
        staged_closed()
        assert get("mounted", "tank/media") == "yes"
        assert files() == before
        machine.succeed("zfs destroy -r tank/media && zfs destroy -r tank/stage")

    with subtest("file-move moves a dataset bigger than the free space"):
        # atime=on relatime=off: every read updates atime, so a read of the source after @enc-final
        # would count as written unless the run's noatime remount holds.
        machine.succeed(
            "zfs create -o mountpoint=/srv/media -o recordsize=1M -o compression=off -o acltype=posix -o xattr=sa"
            " -o atime=on -o relatime=off -o test:keep=1 tank/media"
        )
        populate("45M", "12M", "72M")
        machine.succeed("zpool sync tank")
        used, avail = int(get("used", "tank/media")), int(get("avail", "tank"))
        print(f"tank/media uses {used} bytes; tank has {avail} bytes free")
        assert used > avail, (used, avail)
        assert "free space" in machine.fail("zfs-encrypt-migrate seed --stage tank/stage --min-free 32M tank/media 2>&1")
        xattrs_acls = manifest("move.before")
        out = machine.succeed(f"{CLI} --batch-size 64M {FINAL} --delete-source tank/media 2>&1")
        print(out)
        assert "file-move done" in out, out
        assert manifest("move.after") == xattrs_acls
        same("move.before", "move.after")
        assert get("encryption", "tank/media") == "aes-256-gcm"
        assert get("encryptionroot", "tank/media") == "tank/media"
        assert get("keylocation", "tank/media") == "file:///run/final.key"
        assert machine.succeed("findmnt -n -o SOURCE /srv/media").strip() == "tank/media"
        for prop, value in (
            ("mountpoint", "/srv/media"),
            ("recordsize", "1048576"),
            ("compression", "off"),
            ("acltype", "posix"),
            ("xattr", "sa"),
            ("atime", "on"),
            ("relatime", "off"),
            ("test:keep", "1"),
        ):
            got = machine.succeed(f"zfs get -H -p -o value,source {prop} tank/media").split()
            assert got == [value, "local"], (prop, got)
        assert "zfs-encrypt-migrate:" not in machine.succeed("zfs get -H -o property all tank/media")
        machine.fail("zfs list tank/stage/media")
        # The original keeps only its directories (and @enc-final).
        machine.succeed("zfs set mountpoint=/mnt/old tank/stage/old/media && zfs mount tank/stage/old/media")
        assert machine.succeed("find /mnt/old ! -type d") == ""
        dirs = lambda root: machine.succeed(f"find {root} -type d | wc -l").strip()
        assert dirs("/mnt/old") == dirs("/srv/media")
        machine.succeed("zfs destroy -r tank/media && zfs destroy -r tank/stage")

    with subtest("file-move resumes after an interrupt"):
        machine.succeed("zfs create -o mountpoint=/srv/media -o acltype=posix tank/media")
        populate("6M", "2M", "10M")
        xattrs_acls = manifest("resume.before")
        total = len(files().splitlines())
        machine.succeed("rm -f /tmp/rsync-calls")
        status, out = machine.execute(f"PATH=${fakeRsync}/bin:$PATH {CLI} --batch-size 8M {FINAL} --delete-source tank/media 2>&1")
        print(out)
        assert status == 130, (status, out)
        assert machine.succeed("cat /tmp/rsync-calls").strip() == "4"
        assert "Rerun the same command to continue." in out, out
        staged_closed()
        # The first batch is gone from the source, the interrupted one is not.
        left = len(files().splitlines())
        assert 0 < left < total, (left, total)
        out = machine.succeed(f"{CLI} --batch-size 8M {FINAL} --delete-source tank/media 2>&1")
        print(out)
        assert "file-move done" in out, out
        assert manifest("resume.after") == xattrs_acls
        same("resume.before", "resume.after")
        assert get("encryptionroot", "tank/media") == "tank/media"
        machine.succeed("zfs destroy -r tank/media && zfs destroy -r tank/stage")

    with subtest("file-move checks the passphrase at a terminal"):
        machine.succeed("zfs create -o mountpoint=/srv/media -o acltype=posix tank/media")
        populate("1M", "1M", "3M")
        xattrs_acls = manifest("prompt.before")
        run = (
            "zfs-encrypt-migrate file-move --stage tank/stage --min-free 32M --batch-size 2M"
            " --keyformat passphrase --final-keylocation prompt --delete-source tank/media"
        )
        plain = lambda out: re.sub(r"\x1b\[[0-9;]*m", "", out).replace("\r", "")
        status, out = machine.execute(f"${atTerminal} wrong-passphrase '{run}'")
        print(plain(out))
        assert status == 1, (status, out)
        assert plain(out).count("Enter passphrase") == 3, out
        assert "did not load from prompt; nothing was deleted" in plain(out), out
        staged_closed()
        assert manifest("prompt.wrong") == xattrs_acls
        same("prompt.before", "prompt.wrong")
        status, out = machine.execute(f"${atTerminal} test-passphrase-123 '{run}'")
        print(plain(out))
        assert status == 0 and "file-move done" in plain(out), (status, out)
        assert manifest("prompt.after") == xattrs_acls
        same("prompt.before", "prompt.after")
        assert get("keylocation", "tank/media") == "prompt"
        machine.succeed("zfs load-key -n tank/media < /run/zfs-encrypt-migrate.key")
        machine.fail("printf wrong-passphrase | zfs load-key -n tank/media")
  '';
}
