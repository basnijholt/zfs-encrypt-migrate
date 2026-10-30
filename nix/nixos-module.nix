{ config, lib, ... }:

let
  cfg = config.services.zfs-encrypt-migrate;
  auditCfg = cfg.audit;

  auditArgs =
    lib.concatMap (name: [ "--exact" name ]) auditCfg.exact
    ++ lib.concatMap (name: [ "--subtree" name ]) auditCfg.subtrees
    ++ auditCfg.pools;
in
{
  options.services.zfs-encrypt-migrate = {
    enable = lib.mkEnableOption "the zfs-encrypt-migrate command-line tool";

    package = lib.mkOption {
      type = lib.types.package;
      description = ''
        Package providing the zfs-encrypt-migrate executable.

        The flake's nixosModules.default defaults this to the pinned
        zfs-encrypt-migrate package. When importing nix/nixos-module.nix
        directly, set it explicitly. The package does not bundle zfs: it uses
        the zfs and zpool of the host, which match the host's kernel module.
      '';
    };

    audit = {
      enable = lib.mkEnableOption "the scheduled audit for unencrypted datasets";

      pools = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "tank" ];
        description = "Pools to audit. Must be non-empty when the audit is enabled.";
      };

      exact = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "tank" ];
        description = "Datasets allowed to stay unencrypted, matched by whole name.";
      };

      subtrees = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "tank/stage" ];
        description = "Datasets allowed to stay unencrypted together with all their descendants.";
      };

      onCalendar = lib.mkOption {
        type = lib.types.str;
        default = "daily";
        description = "systemd calendar expression for the audit timer.";
      };

      onFailure = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "notify-failure@%n.service" ];
        description = "Units to start when the audit fails, that is when unencrypted datasets are found.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !auditCfg.enable || auditCfg.pools != [ ];
        message = "services.zfs-encrypt-migrate.audit.pools must name at least one pool when the audit is enabled.";
      }
    ];

    environment.systemPackages = [ cfg.package ];

    systemd.services.zfs-encrypt-migrate-audit = lib.mkIf auditCfg.enable {
      description = "Audit ZFS pools for unencrypted datasets";
      wants = [ "zfs-import.target" ];
      after = [ "zfs-import.target" ];
      # The host's own zfs: the pinned one may not match the loaded kernel module.
      path = [ config.boot.zfs.package ];
      unitConfig.OnFailure = auditCfg.onFailure;
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${lib.getExe cfg.package} audit ${lib.escapeShellArgs auditArgs}";
        # Needs root and read-write /dev/zfs, so no PrivateDevices and no User=.
        # The audit only reads pool state, so the rest of the system can be read-only.
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
        ProtectControlGroups = true;
        RestrictSuidSgid = true;
        LockPersonality = true;
      };
    };

    systemd.timers.zfs-encrypt-migrate-audit = lib.mkIf auditCfg.enable {
      description = "Audit ZFS pools for unencrypted datasets";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = auditCfg.onCalendar;
        Persistent = true;
      };
    };
  };
}
