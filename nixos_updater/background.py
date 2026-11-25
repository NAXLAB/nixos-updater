"""Unattended run, used by `nixos-updater --scheduled`: no window, no GTK.

Updates the flake (and builds, if auto-build is on), then sends a desktop notification. Clicking it opens
the app with --prompt, which lands on the confirm-build or confirm-switch dialog. Nothing is ever switched
without that confirmation, and `sudo` is skipped here (see engine.HEADLESS_SHIM).
"""
import shutil
import subprocess
import sys
import time

from .engine import Engine

NOTIFY_WAIT = 12 * 3600   # how long to wait for a click before giving up


def notify(title, body, clickable=True):
    """Show a notification. Returns True if the user clicked it (needs libnotify >= 0.8 for --action)."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    base = [exe, "--app-name=NixOS Updater", "--icon=dev.local.NixosUpdater"]
    if not clickable:
        try:
            subprocess.run(base + [title, body], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        return False
    try:
        r = subprocess.run(base + ["--action=default=Open", "--wait", title, body],
                           capture_output=True, text=True, timeout=NOTIFY_WAIT)
        if r.returncode != 0:   # older notify-send without --action: plain, non-clickable notification
            subprocess.run(base + [title, body], capture_output=True, timeout=30)
            return False
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "default"


def open_ui():
    """Open the window on the pending confirm dialog, and carry on (the window outlives us)."""
    subprocess.Popen([sys.executable, "-m", "nixos_updater", "--prompt"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch_ui():
    # Blocks until the window is closed: as a systemd service, our process group dies when we exit.
    subprocess.run([sys.executable, "-m", "nixos_updater", "--prompt"])


def run(cfg, launch=launch_ui):
    cfg.last_check = time.time()   # also on failure, so a broken setup notifies once per period, not hourly
    cfg.save()
    eng = Engine(cfg, on_change=lambda: None, ask_password=lambda: None, offer_switch=lambda: None, headless=True)
    eng.start("check")
    eng.thread.join()
    s = eng.s
    if eng.busy:                    # the window is already doing something
        return 0
    changed = sum(i.changed for i in s.inputs)
    if s.error:
        title, body = "Update failed", s.error
    elif s.built:
        title, body = "Ready to switch", f"Generation {s.new_generation} is built. Click to review and switch."
    elif changed:
        title, body = "Flake updated", f"{changed} flake input{'s' if changed != 1 else ''} updated. Click to rebuild & switch."
    else:
        return 0                    # up to date: stay quiet
    if notify(title, body):
        launch()
    return 0
