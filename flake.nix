{
  description = "Migrate existing unencrypted OpenZFS datasets to native encryption in place";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          pythonPackages = pkgs.python3Packages;
          version = builtins.replaceStrings [ "\n" "\r" ] [ "" "" ] (builtins.readFile ./VERSION);
        in
        {
          # zfs is deliberately not wrapped onto PATH: a flake-pinned zfs can
          # differ from the host's kernel module, so the CLI uses the system one.
          default = pythonPackages.buildPythonApplication {
            pname = "zfs-encrypt-migrate";
            inherit version;

            pyproject = true;
            src = self;

            build-system = with pythonPackages; [
              hatch-vcs
              hatchling
            ];

            dependencies = with pythonPackages; [
              rich
              typer
            ];

            env.SETUPTOOLS_SCM_PRETEND_VERSION = version;

            pythonImportsCheck = [ "zfs_encrypt_migrate" ];
            doCheck = false;

            passthru.isZfsEncryptMigratePackage = true;

            meta = {
              description = "Migrate existing unencrypted OpenZFS datasets to native encryption in place";
              homepage = "https://github.com/basnijholt/zfs-encrypt-migrate";
              license = pkgs.lib.licenses.mit;
              mainProgram = "zfs-encrypt-migrate";
            };
          };
        }
      );

      nixosModules.default =
        { lib, pkgs, ... }:
        {
          imports = [ ./nix/nixos-module.nix ];
          services.zfs-encrypt-migrate.package =
            lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };

      checks = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          eval = nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.default
              {
                system.stateVersion = "26.05";
                # Minimal bootable-ish config so forcing `config.assertions`
                # below only surfaces real module failures.
                boot.loader.grub.enable = false;
                fileSystems."/" = {
                  device = "none";
                  fsType = "tmpfs";
                };
                services.zfs-encrypt-migrate = {
                  enable = true;
                  audit = {
                    enable = true;
                    pools = [ "tank" ];
                    exact = [ "tank" ];
                    subtrees = [ "tank/stage" ];
                    onFailure = [ "notify-failure@%n.service" ];
                  };
                  package = pkgs.writeShellScriptBin "zfs-encrypt-migrate" "exit 0";
                };
              }
            ];
          };
          # The unit only exists in the evaluated config; module assertions
          # fire only when building the system toplevel, so check them here.
          failedAssertions = builtins.filter (a: !a.assertion) eval.config.assertions;
          auditService =
            if failedAssertions == [ ] then
              eval.config.systemd.services.zfs-encrypt-migrate-audit
            else
              throw (nixpkgs.lib.concatMapStringsSep "\n" (a: a.message) failedAssertions);
          auditTimer = eval.config.systemd.timers.zfs-encrypt-migrate-audit;
        in
        {
          module = pkgs.runCommand "zfs-encrypt-migrate-module-check" { } ''
            grep -q ' audit --exact tank --subtree tank/stage tank$' ${pkgs.writeText "exec-start" auditService.serviceConfig.ExecStart}
            grep -qx 'notify-failure@%n.service' ${pkgs.writeText "on-failure" (builtins.concatStringsSep "\n" auditService.unitConfig.OnFailure)}
            grep -qx 'daily' ${pkgs.writeText "on-calendar" auditTimer.timerConfig.OnCalendar}
            touch "$out"
          '';
        }
        # End-to-end VM test against real OpenZFS. Only x86_64-linux runs it in
        # CI (KVM there); on other systems it would need cross-emulation.
        // nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
          integration = import ./nix/integration-test.nix {
            inherit pkgs;
            module = self.nixosModules.default;
            package = self.packages.${system}.default;
          };
        }
      );
    };
}
