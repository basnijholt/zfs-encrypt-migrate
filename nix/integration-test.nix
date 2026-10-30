# End-to-end integration test against *real* OpenZFS.
#
# The unit suite drives a simulated zfs, so it proves the migration logic given
# assumed `zfs` behavior. This test proves those assumptions: it boots a VM with
# a real kernel and real OpenZFS, runs every migration command against loopback
# pools (seed, catchup, cutover, the tree commands, the audit) and rehearses the
# manual pool rebuild that a pool root needs, using raw zfs commands. The NixOS
# module is exercised through its audit unit. The CLI runs against the guest's
# own zfs, the same way the module's units use the host's.
{
  pkgs,
  module,
  package,
}:
pkgs.testers.runNixOSTest {
  name = "zfs-encrypt-migrate-integration";

  nodes.machine =
    { ... }:
    {
      imports = [ module ];

      # Real ZFS in the guest; its zfs/zpool land on the system PATH.
      boot.supportedFilesystems = [ "zfs" ];
      # ZFS refuses to import without a hostId; the value is arbitrary.
      networking.hostId = "8425e349";

      environment.systemPackages = [
        pkgs.gawk
        pkgs.util-linux
      ];

      services.zfs-encrypt-migrate = {
        enable = true;
        inherit package;
        audit = {
          enable = true;
          pools = [ "tank" ];
          exact = [ "tank" ];
          subtrees = [
            "tank/six"
            "tank/plain"
            "tank/stage"
            "tank/allowed"
          ];
        };
      };

      # Backing disks for the tank (/dev/vdb) and ssd (/dev/vdc) pools.
      virtualisation.emptyDiskImages = [
        4096
        1024
      ];
      virtualisation.memorySize = 2048;
    };

  testScript = ''
    machine.wait_for_unit("multi-user.target")
    machine.succeed("modprobe zfs")
    # The CLI uses the guest's zfs, so record which OpenZFS these assertions ran against.
    print(machine.succeed("zfs version"))
    assert "-u" in machine.succeed("zfs rename 2>&1 || true")
    machine.succeed("zpool create -O mountpoint=/tank tank /dev/vdb")
    machine.succeed("zpool create -O mountpoint=/ssd ssd /dev/vdc")

    M = "ZFS_ENCRYPT_MIGRATE_MIN_FREE=1M"
    S = "--stage tank/stage"
    get = lambda p, d: machine.succeed(f"zfs get -H -o value {p} {d}").strip()

    with subtest("seed refuses without key file"):
        machine.succeed("zfs create tank/share && echo a > /tank/share/a && zfs snapshot tank/share@autosnap_1")
        assert "key file" in machine.fail(f"{M} zfs-encrypt-migrate seed {S} tank/share 2>&1")
    machine.succeed("printf test-passphrase-123 > /run/zfs-encrypt-migrate.key && chmod 600 /run/zfs-encrypt-migrate.key")

    with subtest("seed refuses below free-space floor"):
        assert "free space" in machine.fail(f"ZFS_ENCRYPT_MIGRATE_MIN_FREE=1T zfs-encrypt-migrate seed {S} tank/share 2>&1")

    with subtest("M1 swap keeps data, snapshots and path"):
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} tank/share")
        machine.succeed("echo b > /tank/share/b")
        machine.succeed(f"{M} zfs-encrypt-migrate cutover {S} tank/share")
        assert get("encryption", "tank/share") == "aes-256-gcm"
        assert get("encryptionroot", "tank/share") == "tank/share"
        assert get("keylocation", "tank/share") == "prompt"
        assert get("mounted", "tank/share") == "yes"
        machine.succeed("test -f /tank/share/a && test -f /tank/share/b")
        snaps = machine.succeed("zfs list -H -t snapshot -o name tank/share")
        for s in ("@autosnap_1", "@enc-seed", "@enc-final"):
            assert f"tank/share{s}" in snaps
        assert get("encryption", "tank/stage/old/share") == "off"
        assert get("canmount", "tank/stage/old/share") == "noauto"
        assert get("readonly", "tank/stage/old/share") == "on"
        assert get("mounted", "tank/stage/old/share") == "no"

    with subtest("cutover handles children, legacy mountpoints and stays unmounted in old"):
        machine.succeed("zfs create tank/data && zfs create tank/data/child && echo c > /tank/data/child/c")
        machine.succeed("zfs create -o mountpoint=legacy tank/data/legacy")
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} tank/data && {M} zfs-encrypt-migrate cutover {S} tank/data")
        assert get("encryptionroot", "tank/data/child") == "tank/data"
        assert get("mounted", "tank/data/child") == "yes"
        assert get("mounted", "tank/data/legacy") == "no"
        assert get("mounted", "tank/stage/old/data/child") == "no"
        machine.succeed("test -f /tank/data/child/c")

    with subtest("a child created after seed is encrypted at cutover"):
        machine.succeed("zfs create tank/late")
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} tank/late")
        machine.succeed("zfs create tank/late/new && echo n > /tank/late/new/n")
        machine.succeed(f"{M} zfs-encrypt-migrate cutover {S} tank/late")
        assert get("encryptionroot", "tank/late/new") == "tank/late"
        assert get("mounted", "tank/late/new") == "yes"
        machine.succeed("test -f /tank/late/new/n")

    with subtest("seed refuses already-encrypted dataset"):
        assert "already encrypted" in machine.fail(f"{M} zfs-encrypt-migrate seed {S} tank/share 2>&1")

    with subtest("one migration in flight"):
        machine.succeed("zfs create tank/one && zfs create tank/two")
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} tank/one")
        assert "in flight" in machine.fail(f"{M} zfs-encrypt-migrate seed {S} tank/two 2>&1")
        machine.succeed(f"{M} zfs-encrypt-migrate cutover {S} tank/one")

    with subtest("cutover is re-runnable after catchup"):
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} tank/two && {M} zfs-encrypt-migrate catchup {S} tank/two")
        machine.succeed(f"{M} zfs-encrypt-migrate cutover {S} tank/two")
        assert get("encryptionroot", "tank/two") == "tank/two"

    with subtest("cutover refuses writes made after catchup"):
        machine.succeed("zfs create tank/three")
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} tank/three && {M} zfs-encrypt-migrate catchup {S} tank/three")
        machine.succeed("echo late > /tank/three/late")
        out = machine.fail(f"{M} zfs-encrypt-migrate cutover {S} tank/three 2>&1")
        assert "written since catchup" in out and "zfs destroy -r tank/three@enc-final" in out
        assert "zfs destroy -r tank/stage/three@enc-final" in out
        machine.succeed("zfs destroy -r tank/three@enc-final && zfs destroy -r tank/stage/three@enc-final")
        machine.succeed(f"{M} zfs-encrypt-migrate cutover {S} tank/three")
        machine.succeed("test -f /tank/three/late")

    with subtest("snapshots taken between seed and cutover survive"):
        machine.succeed("zfs create tank/four && echo f > /tank/four/f")
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} tank/four")
        machine.succeed("zfs snapshot tank/four@autosnap_mid")
        machine.succeed(f"{M} zfs-encrypt-migrate cutover {S} tank/four")
        assert "tank/four@autosnap_mid" in machine.succeed("zfs list -H -t snapshot -o name tank/four")

    with subtest("catchup refuses an existing enc-final"):
        machine.succeed("zfs create tank/five")
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} tank/five && {M} zfs-encrypt-migrate catchup {S} tank/five")
        out = machine.fail(f"{M} zfs-encrypt-migrate catchup {S} tank/five 2>&1")
        assert "zfs destroy -r tank/five@enc-final" in out
        assert "zfs destroy -r tank/stage/five@enc-final" in out
        machine.succeed(f"{M} zfs-encrypt-migrate cutover {S} tank/five")

    with subtest("failed catchup leaves the staged key unloaded"):
        machine.succeed("zfs create tank/six && zfs create tank/six/child")
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} tank/six")
        machine.succeed("zfs snapshot tank/six/child@enc-final")
        machine.fail(f"{M} zfs-encrypt-migrate catchup {S} tank/six")
        assert get("keystatus", "tank/stage/six") == "unavailable"
        machine.succeed("zfs destroy -r tank/stage/six && zfs destroy -r tank/six@enc-seed && zfs destroy tank/six/child@enc-final")

    with subtest("pool root: seed and catchup allowed, cutover refused"):
        machine.succeed("zfs create ssd/docker && echo d > /ssd/docker/d")
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} ssd")
        assert get("keystatus", "tank/stage/ssd") == "unavailable"
        machine.succeed("zfs mount -a")
        assert machine.succeed("findmnt -n -o SOURCE /ssd").strip() == "ssd"
        machine.succeed(f"{M} zfs-encrypt-migrate catchup {S} ssd")
        assert get("encryptionroot", "tank/stage/ssd/docker") == "tank/stage/ssd"
        g = lambda d: get("guid", f"{d}@enc-final")
        assert g("ssd/docker") == g("tank/stage/ssd/docker")
        assert "pool root" in machine.fail(f"{M} zfs-encrypt-migrate cutover {S} ssd 2>&1")
        machine.succeed("zfs destroy -r tank/stage/ssd")

    with subtest("M2 tree move preserves child properties and snapshots"):
        machine.succeed("zfs create tank/backups && echo p > /tank/backups/p")
        machine.succeed("zfs create tank/backups/b1 && echo x > /tank/backups/b1/x")
        machine.succeed("zfs create -o mountpoint=none -o readonly=on tank/backups/b2 && zfs snapshot tank/backups/b2@autosnap_x")
        machine.succeed("zfs create -o readonly=off -o mountpoint=/tank/b2c tank/backups/b2/c && echo g > /tank/b2c/g && zfs snapshot tank/backups/b2/c@autosnap_c")
        machine.succeed("zfs create -V 16M tank/backups/v")
        machine.succeed(f"{M} zfs-encrypt-migrate tree-seed {S} tank/backups")
        assert get("keystatus", "tank/stage/backups") == "unavailable"
        machine.succeed(f"{M} zfs-encrypt-migrate tree-move {S} tank/backups b1 && zfs destroy -r tank/backups/b1")
        machine.succeed(f"{M} zfs-encrypt-migrate tree-move {S} tank/backups b2")
        machine.succeed(f"{M} zfs-encrypt-migrate tree-move {S} tank/backups v")
        assert get("keystatus", "tank/stage/backups") == "unavailable"
        assert "children remain" in machine.fail(f"{M} zfs-encrypt-migrate tree-finish {S} tank/backups 2>&1")
        machine.succeed(f"zfs destroy -r tank/backups/b2 && zfs destroy -r tank/backups/v && {M} zfs-encrypt-migrate tree-finish {S} tank/backups")
        assert get("encryptionroot", "tank/backups") == "tank/backups"
        assert get("keylocation", "tank/backups") == "prompt"
        for d in ("b1", "b2", "b2/c", "v"):
            assert get("encryptionroot", f"tank/backups/{d}") == "tank/backups"
        machine.succeed("test -f /tank/b2c/g")
        assert "tank/backups/b2/c@autosnap_c" in machine.succeed("zfs list -H -t snapshot -o name tank/backups/b2/c")
        assert get("mountpoint", "tank/backups/b2") == "none"
        assert get("readonly", "tank/backups/b2") == "on"
        assert "tank/backups/b2@autosnap_x" in machine.succeed("zfs list -H -t snapshot -o name tank/backups/b2")
        machine.succeed("test -f /tank/backups/p && test -f /tank/backups/b1/x")

    with subtest("M3 rehearsal: rebuild ssd encrypted and send back"):
        names = machine.succeed("zfs list -rH -o name tank/stage").split()
        assert all(n in ("tank/stage", "tank/stage/old") or n.startswith("tank/stage/old/") for n in names), names
        machine.succeed("zfs destroy -r ssd@enc-seed && zfs destroy -r ssd@enc-final")
        machine.succeed("zfs create ssd/docker/app && echo a > /ssd/docker/app/a && zfs snapshot ssd/docker/app@autosnap_app")
        machine.succeed("zfs create -V 16M ssd/vol")
        machine.succeed(f"{M} zfs-encrypt-migrate seed {S} ssd")
        machine.succeed("zfs create ssd/docker/new && echo n > /ssd/docker/new/n")
        machine.succeed(f"{M} zfs-encrypt-migrate catchup {S} ssd")
        assert get("encryptionroot", "tank/stage/ssd/docker/new") == "tank/stage/ssd"
        machine.succeed("udevadm settle && zpool destroy ssd")
        machine.succeed("zpool create -f -O encryption=aes-256-gcm -O keyformat=passphrase -O keylocation=file:///run/zfs-encrypt-migrate.key -O mountpoint=/ssd ssd /dev/vdc")
        # Runbook Phase 3 step 6, verbatim.
        machine.succeed(
            'for d in $(zfs list -H -d1 -t filesystem,volume -o name tank/stage/ssd | tail -n +2); do c=''${d#tank/stage/}; '
            'zfs send -R -w "$d@enc-final" | zfs recv -u "$c" && zfs load-key -L file:///run/zfs-encrypt-migrate.key "$c" && zfs change-key -i "$c"; done'
        )
        assert get("keystatus", "tank/stage/ssd") == "unavailable"
        machine.succeed("zfs set keylocation=prompt ssd && zfs mount -a")
        assert machine.succeed("zfs get -rH -o name,value encryptionroot ssd | awk '$2 != \"ssd\"'") == ""
        for f in ("docker/d", "docker/app/a", "docker/new/n"):
            machine.succeed(f"test -f /ssd/{f}")
        assert machine.succeed("findmnt -n -o SOURCE /ssd/docker").strip() == "ssd/docker"
        assert "ssd/docker/app@autosnap_app" in machine.succeed("zfs list -H -t snapshot -o name ssd/docker/app")
        for d in ("docker", "docker/app", "docker/new", "vol"):
            assert get("guid", f"ssd/{d}@enc-final") == get("guid", f"tank/stage/ssd/{d}@enc-final")
        machine.succeed("zfs destroy -r tank/stage/ssd")

    with subtest("audit lists unencrypted datasets outside the allowlist on the named pools"):
        machine.succeed("zfs create tank/plain")
        machine.succeed("truncate -s 128M /var/zroot.img && zpool create -O mountpoint=none zroot /var/zroot.img && zfs create zroot/home")
        out = machine.fail("zfs-encrypt-migrate audit --exact tank --exact ssd --exact tank/stage --exact tank/stage/old tank ssd")
        assert "tank/plain" in out and "tank/stage/old/share" in out and "zroot" not in out
        assert "tank/share\n" not in out  # encrypted after Task 1
        ok = "zfs-encrypt-migrate audit --exact tank --exact ssd --subtree tank/six --subtree tank/plain --subtree tank/stage --subtree ssd/docker"
        machine.succeed(f"{ok} tank ssd")
        assert "zroot/home" in machine.fail(f"{ok} tank ssd zroot")
        machine.succeed("zpool destroy zroot")
        status, out = machine.execute(f"{ok} tank ssd nopool 2>&1")
        assert status != 0 and "nopool" in out, (status, out)
        assert machine.execute(ok)[0] == 2

    with subtest("module audit unit fails on an unencrypted dataset and passes once allowlisted"):
        machine.succeed("systemctl cat zfs-encrypt-migrate-audit.service zfs-encrypt-migrate-audit.timer")
        machine.succeed("systemctl is-enabled zfs-encrypt-migrate-audit.timer")
        machine.succeed("zfs create tank/rogue")
        machine.fail("systemctl start zfs-encrypt-migrate-audit.service")
        assert "tank/rogue" in machine.succeed("journalctl -u zfs-encrypt-migrate-audit.service --no-pager")
        machine.succeed("zfs rename tank/rogue tank/allowed")
        machine.succeed("systemctl start zfs-encrypt-migrate-audit.service")

  '';
}
