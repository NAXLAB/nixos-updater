"""The detached job runner: `nixos-updater --run JOB`, started by the window.

It runs one job (check / rebuild / switch / ...) in its own session, so closing the window does not stop it.
It publishes its state through runstate.py, which the window reads. When it finishes and no window is open,
it brings the next step to you: it opens the window on the confirm dialog (rebuild / switch), or sends a
notification (switch done, or something failed).

The sudo password arrives on stdin (one line), never on disk or in the command line.
"""
import os
import signal
import sys
import time

from . import runstate
from .background import notify, open_ui
from .engine import Engine


class Writer:
    """Publishes the engine's state to run.json / run.log."""

    def __init__(self, eng, job, job_id, offers):
        self.eng, self.job, self.job_id, self.offers = eng, job, job_id, offers
        self.written = 0
        runstate.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.log = open(runstate.RUN_LOG, "w", encoding="utf-8")   # truncates the previous job's log

    def flush(self, finished=False):
        s = self.eng.s
        lines = s.log[self.written:]
        if lines:
            self.log.write("\n".join(lines) + "\n")
            self.log.flush()
            self.written += len(lines)
        for _ in range(3):   # s.steps can change under us; just retry
            try:
                steps = dict(s.steps)
                break
            except RuntimeError:
                steps = {}
        runstate.write({
            "job_id": self.job_id, "job": self.job, "pid": os.getpid(), "finished": finished,
            "running": s.running, "steps": steps, "error": s.error, "built": s.built, "progress": s.progress,
            "new_generation": s.new_generation, "new_kernel": s.new_kernel, "packages": s.packages,
            "build_seconds": s.build_seconds, "offer": bool(self.offers),
            "password_rejected": self.eng.password_rejected, "updated": time.time()})


def aftermath(eng, job):
    """Job done. If nobody is watching, hand the next step to the user."""
    s = eng.s
    cancelled = bool(s.log) and s.log[-1] == "Cancelled."
    if eng.busy or cancelled or runstate.window_is_open():   # an open window shows the result itself
        return
    if s.error:
        if notify("Update failed" if job in ("check", "rebuild", "rebuild_switch") else "Failed", s.error):
            open_ui()
    elif job.startswith("switch"):
        body = f"Generation {s.generation} is active."
        if s.restart:
            body += f" Restart to run Linux {s.kernel}."
        notify("Switch complete", body, clickable=False)
    elif job != "rollback" and s.phase() in ("ready", "updates"):
        open_ui()   # the window opens on the confirm-switch / confirm-rebuild dialog


def main(cfg, job, job_id, stdin=None):
    password = (stdin or sys.stdin).readline().rstrip("\n") or None
    offers = []
    eng = Engine(cfg, on_change=lambda: None, ask_password=lambda: password,
                 offer_switch=lambda: offers.append(1))
    writer = Writer(eng, job, job_id, offers)
    signal.signal(signal.SIGTERM, lambda *_: eng.cancel())   # the window's Cancel button
    eng.start(job)
    while eng.thread.is_alive():
        writer.flush()
        time.sleep(0.25)
    eng.thread.join()
    writer.flush(finished=True)
    aftermath(eng, job)
    return 0
