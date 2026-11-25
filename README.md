# NixOS Updater

GTK4 / libadwaita front end for **flake update → build → switch**.

## Run
    nix run .                 # build and run
    nix develop               # then: python -m nixos_updater   (runs from source)
    python -m nixos_updater --check    # open and check immediately

NixOS module (installs the app, plus an optional background timer):

    inputs.nixos-updater.url = "path:/home/you/nixos-updater";   # or git
    # in your configuration:
    imports = [ inputs.nixos-updater.nixosModules.default ];
    programs.nixos-updater = {
      enable = true;
      # optional; anything you set here is fixed and greyed out in the window:
      # autoUpdate = true;  autoBuild = true;  checkFrequency = "daily";  checkTime = "03:00";  directory = "/home/you/nixos-config";
    };

## Where things live
| File | What it is |
|---|---|
| `nixos_updater/config.py` | Settings (JSON in `~/.config/nixos-updater/`), schedules |
| `nixos_updater/engine.py` | No GTK. Reads flake.lock/generation/kernel, runs the pipeline on a worker thread |
| `nixos_updater/runner.py` | The detached job runner (`--run`, started by the window). Runs the engine, publishes its state, and brings the next step to you if the window is closed |
| `nixos_updater/remote.py` | The window's view of the engine: starts/cancels the runner and polls its state |
| `nixos_updater/runstate.py` | The files runner and window share (`run.json`, `run.log`, `window.lock`) |
| `nixos_updater/window.py` | All UI. One `_build_*` method per section, one `render()` that copies engine state to widgets. It only watches the runner |
| `nixos_updater/background.py` | The unattended run (`--scheduled`): no window, sends the notification |
| `nixos_updater/__main__.py` | CLI flags (`--check`, `--scheduled`, `--prompt`) |
| `nix/package.nix`, `nix/module.nix`, `flake.nix` | Packaging, NixOS module, dev shell |
| `tests/test_engine.py` | `python -m unittest discover tests` (needs only Python) |

## How it behaves
- **Check for Updates** = update command → if flake.lock changed, build → offer switch dialog.
  **Rebuild** = build only. **Rebuild & Switch** = build → offer switch dialog.
  Switch dialog: *Switch now* / *On next boot* (rewrites `nixos-rebuild switch` to `boot`) / *Not now*.
- **"Updated" inputs** = flake.lock differs from `~/.local/state/nixos-updater/flake.lock.switched`,
  a copy taken after every successful switch (the first run assumes your current lock is what you're running).
  **Roll Back** restores that copy (you set the command, e.g. `cp ~/.local/state/nixos-updater/flake.lock.switched flake.lock`).
- **sudo**: there is no terminal, so commands run under bash with `sudo` redefined as `sudo -S -p ""`.
  The app asks for your password once in a dialog, keeps it in memory only, and feeds it on stdin.
  Wrong password → it asks again next run.
- **Commands:** there are no built-in commands; you enter one for each step under *Update Pipeline* (an empty step
  fails with a message saying so). Example: `nix flake update`, `sudo nixos-rebuild build --flake .#host`,
  `sudo nixos-rebuild switch --flake .#host`.
- **Passwords:** only steps whose command contains `sudo` need one. A step like `nix flake update` that only edits
  `flake.lock` needs write access to your flake directory but no password. If *Build After Updating* is on and your
  build command uses `sudo`, a check asks for the password up front.
- **Garbage collection** runs after a successful switch when it's due (`sudo nix-collect-garbage --delete-older-than Nd`).
- **Background checks** (`--scheduled`, run by the module's user timer every hour): the app exits at once unless a
  check is due per Update Frequency and Update Time (it runs once per period, at the first timer wake-up at or after
  that time). If it is, it runs with **no window**: update, then build if *Build After Updating*
  is on. Then it sends a notification (silent if nothing changed): "Ready to switch", "Flake updated", or
  "Update failed". Clicking it opens the window on the confirm-switch dialog, or on "Rebuild & Switch?" if only the update ran (`--prompt`). With *Build After Updating* off, the background run only updates `flake.lock` (no password needed); the build and switch happen in the window, with your password.
  It never switches by itself.
- Background runs have nobody to ask for a password, so `sudo` is skipped there (the command runs as you). Update and
  build don't need root. If your flake.lock is root-owned from earlier `sudo nix flake update` runs, fix it once:
  `sudo chown $USER flake.lock`.
- A finished build is remembered (`~/.local/state/nixos-updater/pending.json` + `./result`), so opening the window
  later, or after a notification, shows "Ready to Switch" with the confirm dialog. It is discarded if flake.lock changes.
- The window and the background run can't work at the same time (a lock file); the background run just skips.
- **Build After Updating** (on by default): Check for Updates = update, then build if anything changed, then confirm
  the switch. Off: Check stops at "Updates Available"; Rebuild / Rebuild & Switch build on request. Every finished
  build from a check or Rebuild & Switch ends in the confirm-switch dialog.
- **Jobs run detached.** A job from the window (check, rebuild, switch, roll back) runs in its own process
  (`nixos-updater --run`), so closing the window does not stop it; the window only watches `run.json`/`run.log`
  in `~/.local/state/nixos-updater/`, and Cancel signals the runner. The sudo password is handed to the runner on
  stdin (never written to disk) and is asked for again after the window is reopened.
- **When the window is closed, the runner brings the next step to you.** Build finished or update found: it opens
  the window on the Rebuild & Switch / Switch dialog. Switch finished: a notification only (with a restart note if
  the kernel changed). Failure: a notification; clicking it opens the window. If the window is open, it shows the
  dialog itself.
- Needs `notify-send` (libnotify >= 0.8 for clickable notifications); the package puts it on PATH.
- Needs libadwaita >= 1.5 (AlertDialog, AboutDialog) and GTK >= 4.12.
