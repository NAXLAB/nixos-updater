{ lib, stdenv, python3, wrapGAppsHook4, gobject-introspection, gtk4, libadwaita, glib, adwaita-icon-theme, libnotify }:

let
  python = python3.withPackages (ps: [ ps.pygobject3 ]);
in
stdenv.mkDerivation {
  pname = "nixos-updater";
  version = "0.1.0";
  src = ../.;

  # wrapGAppsHook4 + gobject-introspection set GI_TYPELIB_PATH / XDG_DATA_DIRS in the wrapper.
  nativeBuildInputs = [ wrapGAppsHook4 gobject-introspection ];
  buildInputs = [ gtk4 libadwaita glib adwaita-icon-theme ];

  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin $out/share/nixos-updater $out/share/applications
    cp -r nixos_updater $out/share/nixos-updater/
    cp data/dev.local.NixosUpdater.desktop $out/share/applications/
    cp -r data/icons $out/share/icons
    cat > $out/bin/nixos-updater <<WRAP
    #!/bin/sh
    export PYTHONPATH=$out/share/nixos-updater:\$PYTHONPATH
    export PATH=${lib.makeBinPath [ libnotify ]}:\$PATH   # notify-send, for background notifications
    exec ${python}/bin/python3 -m nixos_updater "\$@"
    WRAP
    chmod +x $out/bin/nixos-updater
    runHook postInstall
  '';

  meta = {
    description = "GUI for flake update, rebuild and switch on NixOS";
    mainProgram = "nixos-updater";
    platforms = lib.platforms.linux;
  };
}
