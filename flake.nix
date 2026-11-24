{
  description = "NixOS Updater: GTK4/libadwaita front end for flake update -> build -> switch";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forAll (pkgs: rec {
        nixos-updater = pkgs.callPackage ./nix/package.nix { };
        default = nixos-updater;
      });

      apps = forAll (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/nixos-updater";
        };
      });

      # `nix develop`, then `python -m nixos_updater` runs straight from the source tree.
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          nativeBuildInputs = [ pkgs.gobject-introspection ];
          buildInputs = [
            (pkgs.python3.withPackages (ps: [ ps.pygobject3 ]))
            pkgs.gtk4 pkgs.libadwaita pkgs.adwaita-icon-theme
          ];
          shellHook = ''
            export XDG_DATA_DIRS="$XDG_DATA_DIRS:${pkgs.gtk4}/share/gsettings-schemas/${pkgs.gtk4.name}:${pkgs.adwaita-icon-theme}/share"
          '';
        };
      });

      nixosModules.default = import ./nix/module.nix self;
    };
}
