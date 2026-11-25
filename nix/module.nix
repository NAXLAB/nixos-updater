# Usage in your system flake:
#   imports = [ nixos-updater.nixosModules.default ];
#   programs.nixos-updater.enable = true;
#
# Any option left at `null` stays adjustable in the app's window. An option you set here is fixed:
# the matching control in the window is greyed out.
self:
{ config, lib, pkgs, ... }:
let
  cfg = config.programs.nixos-updater;

  # Handed to the app as /etc/nixos-updater/managed.json. The names are the app's own setting names.
  managed = lib.filterAttrs (_: v: v != null) {
    auto_update = cfg.autoUpdate;
    auto_build = cfg.autoBuild;
    check_frequency = cfg.checkFrequency;
    check_time = cfg.checkTime;
    directory = cfg.directory;
  };
in
{
  options.programs.nixos-updater = {
    enable = lib.mkEnableOption "NixOS Updater";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "The nixos-updater package to install.";
    };

    autoUpdate = lib.mkOption {
      type = lib.types.nullOr lib.types.bool;
      default = null;
      description = ''
        Whether the background check runs at all (on the schedule in checkFrequency). If false, nothing runs
        unless you start a check from the window. null leaves the choice to the toggle in the app.
      '';
    };

    autoBuild = lib.mkOption {
      type = lib.types.nullOr lib.types.bool;
      default = null;
      description = ''
        Whether to start building as soon as a flake update finds changes (in the background and in the window).
        If false, an update only reports "updates available" and waits for you to ask for the build.
        null leaves the choice to the toggle in the app.
      '';
    };

    checkFrequency = lib.mkOption {
      type = lib.types.nullOr (lib.types.enum [ "daily" "weekly" "monthly" ]);
      default = null;
      description = "How often the background check runs. null leaves the choice to the app.";
    };

    checkTime = lib.mkOption {
      type = lib.types.nullOr (lib.types.strMatching "([01][0-9]|2[0-3]):[0-5][0-9]");
      default = null;
      example = "03:00";
      description = ''
        Time of day (HH:MM, 24 h) from which the background check may run. The timer only wakes up every
        timerInterval, so the check starts at the first wake-up at or after this time. null leaves it to the app.
      '';
    };

    directory = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/home/me/nixos-config";
      description = "The flake directory to update and build. null leaves the choice to the app.";
    };

    scheduled = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Install a user timer that runs `nixos-updater --scheduled`. That runs without a window: if a check is due
        (see checkFrequency) it updates and, if autoBuild is on, builds, then sends a notification. Clicking the
        notification opens the app on the confirm-switch (or confirm-build) dialog. Nothing is switched without that.
      '';
    };

    timerInterval = lib.mkOption {
      type = lib.types.str;
      default = "hourly";
      description = "systemd OnCalendar value for how often the timer wakes up to ask the app whether a check is due.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];

    environment.etc."nixos-updater/managed.json" = lib.mkIf (managed != { }) {
      text = builtins.toJSON managed;
    };

    systemd.user.services.nixos-updater = lib.mkIf cfg.scheduled {
      description = "NixOS Updater scheduled check";
      # bash, nix and nixos-rebuild must be on PATH (user services start with almost none).
      environment.PATH = lib.mkForce "/run/wrappers/bin:/run/current-system/sw/bin";
      # Stays running while a notification waits for a click, and while the window it opens stays open.
      serviceConfig.ExecStart = "${cfg.package}/bin/nixos-updater --scheduled";
    };
    systemd.user.timers.nixos-updater = lib.mkIf cfg.scheduled {
      wantedBy = [ "timers.target" ];
      timerConfig = { OnCalendar = cfg.timerInterval; Persistent = true; };
    };
  };
}
