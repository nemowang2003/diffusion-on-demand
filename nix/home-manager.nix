{mkPackage}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.diffusion-on-demand;
  toml = pkgs.formats.toml {};
  configuration = toml.generate "diffusion-on-demand.toml" cfg.settings;
in {
  options.services.diffusion-on-demand = {
    enable = lib.mkEnableOption "on-demand SDXL generation";
    package = lib.mkOption {
      type = lib.types.package;
      default = mkPackage {inherit pkgs;};
      description = "Inference package; use the wsl package on NixOS WSL.";
    };
    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Bind address.";
    };
    port = lib.mkOption {
      type = lib.types.ints.between 0 65535;
      default = 0;
      description = "Port; zero allocates a free port.";
    };
    endpoint-file = lib.mkOption {
      type = lib.types.str;
      default = "%t/diffusion-on-demand/endpoint";
      description = "Actual endpoint discovery file.";
    };
    environment-file = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Optional file for DIFFUSION_ON_DEMAND_API_KEY and HF_TOKEN. Keep secrets outside the Nix store.";
    };
    settings = lib.mkOption {
      type = toml.type;
      default = {};
      description = "Model path, auth_required, offload, idle_seconds, timeouts, queue bounds and prompt defaults.";
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = pkgs.stdenv.hostPlatform.isLinux;
        message = "diffusion-on-demand requires Linux/systemd";
      }
    ];
    home.packages = [cfg.package];
    xdg.configFile."diffusion-on-demand/config.toml".source = configuration;
    systemd.user.services.diffusion-on-demand = {
      Unit.Description = "On-demand local SDXL generation API";
      Service =
        {
          Type = "notify";
          NotifyAccess = "main";
          ExecStart = "${cfg.package}/bin/diffusion-on-demand --config ${configuration} serve " + lib.escapeShellArgs ["--host" cfg.host "--port" (toString cfg.port) "--endpoint-file" cfg.endpoint-file];
          Restart = "on-failure";
          RestartSec = 3;
          KillMode = "control-group";
          TimeoutStartSec = 90;
          TimeoutStopSec = 45;
          RuntimeDirectory = "diffusion-on-demand";
          RuntimeDirectoryMode = "0700";
          UMask = "0077";
        }
        // lib.optionalAttrs (cfg.environment-file != null) {EnvironmentFile = cfg.environment-file;};
      Install.WantedBy = ["default.target"];
    };
  };
}
