import argparse
import os
import sys

from .config import Config


def main():
    p = argparse.ArgumentParser(prog="nixos-updater", description="flake update → build → switch, with a GUI")
    p.add_argument("--check", action="store_true", help="open the window and check for updates immediately")
    p.add_argument("--scheduled", action="store_true",
                   help="unattended: if a check is due, run it in the background and send a notification "
                        "(used by the systemd timer)")
    p.add_argument("--prompt", action="store_true",
                   help="open the window on the pending confirm-switch / confirm-build dialog "
                        "(used when a notification is clicked)")
    p.add_argument("--run", metavar="JOB", help=argparse.SUPPRESS)   # internal: the detached job runner
    p.add_argument("--job-id", help=argparse.SUPPRESS)
    args = p.parse_args()

    cfg = Config.load()
    if args.run:
        from .runner import main as run_job   # no GTK needed
        return run_job(cfg, args.run, args.job_id or "manual")
    if args.scheduled:
        if not cfg.check_due():
            return 0
        from .background import run   # no GTK needed
        return run(cfg)
    # Avoids "Gdk-WARNING: vkAcquireNextImageKHR(): ... VK_SUBOPTIMAL_KHR" from GTK's Vulkan renderer.
    # Harmless either way; override with GSK_RENDERER=vulkan if you want it back.
    os.environ.setdefault("GSK_RENDERER", "ngl")
    from .window import App  # imported late so --scheduled's early exit doesn't need GTK
    return App(cfg, auto_check=args.check, prompt=args.prompt).run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
