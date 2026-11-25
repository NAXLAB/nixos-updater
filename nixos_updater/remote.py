"""The window's view of the engine. Same surface as Engine (.s, .start, .cancel, .refresh), but jobs run in a
detached runner process (runner.py); this class only starts it, cancels it, and reads its state from disk."""
import os
import signal
import subprocess
import sys
import threading
import time

from . import runstate
from .config import STATE_DIR
from .engine import Engine

STALE_AFTER = 30   # seconds without a heartbeat from a runner that claims to be running


class Remote(Engine):
    def __init__(self, cfg, ask_password, on_offer):
        """ask_password(callback): show a dialog, then call callback(password or None).
        on_offer(): a job this window watched finished and wants the switch confirmed."""
        super().__init__(cfg, on_change=lambda: None, ask_password=None, offer_switch=lambda: None)
        self._ask, self._on_offer = ask_password, on_offer
        self._job_id, self._log_offset = None, 0
        self._seen_running, self._offered = set(), set()
        self.poll()

    # -- starting / cancelling ----------------------------------------------
    def start(self, job):
        if self.s.running:
            return
        if self._needs_sudo(job) and self._password is None:
            def got(password):
                if password is not None:
                    self._password = password
                    self._launch(job)
            self._ask(got)
        else:
            self._launch(job)

    def _needs_sudo(self, job):
        steps = {"check": ["update"] + (["build"] if self.cfg.auto_build else []), "rebuild": ["build"],
                 "rebuild_switch": ["build"], "switch": ["switch"], "switch_boot": ["switch"],
                 "rollback": ["rollback"]}[job]
        if job.startswith("switch") and self.cfg.gc_due():
            steps.append("gc")
        cmds = [self.cfg.gc_command() if k == "gc" else self.cfg.command(k) for k in steps]
        return any("sudo" in c for c in cmds)

    def _launch(self, job):
        if self.s.running:
            return
        job_id = str(time.time())
        # Placeholder state so the window shows "Starting…" at once (and nothing else can start). The runner
        # replaces it within a moment. pid is ours for now: cancel() ignores that.
        runstate.write({"job_id": job_id, "job": job, "pid": os.getpid(), "finished": False,
                        "running": "starting", "steps": {}, "error": None, "built": self.s.built,
                        "progress": None, "updated": time.time()})
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                [sys.executable, "-m", "nixos_updater", "--run", job, "--job-id", job_id],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=open(STATE_DIR / "runner.err", "ab"),
                start_new_session=True)   # its own session: it survives this window closing
            proc.stdin.write(((self._password or "") + "\n").encode())
            proc.stdin.close()
            threading.Thread(target=proc.wait, daemon=True).start()   # reap it when it ends
        except OSError as e:
            runstate.write({"job_id": job_id, "job": job, "pid": os.getpid(), "finished": True, "running": None,
                            "steps": {}, "error": f"Could not start the runner: {e}", "built": self.s.built,
                            "progress": None, "updated": time.time()})
        self.poll()

    def cancel(self):
        data = runstate.read() or {}
        pid = data.get("pid")
        if self.s.running and pid and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)   # the runner turns this into Engine.cancel()
            except OSError:
                pass

    # -- watching -----------------------------------------------------------
    def _signature(self):
        s = self.s
        return (s.running, tuple(sorted(s.steps.items())), s.error, s.built, s.progress, len(s.log),
                tuple((i.name, i.old, i.new) for i in s.inputs), s.rollback_available, s.generation,
                s.kernel, s.restart, s.new_generation, s.new_kernel, s.packages)

    def poll(self):
        """Pull the runner's state from disk. Returns True if anything the window shows changed."""
        before = self._signature()
        data = runstate.read()
        if data:
            self._apply(data)
        self.refresh()
        if not self.s.built:
            self._load_pending()   # a build made by the background check, say
        return self._signature() != before

    def _apply(self, data):
        s = self.s
        if data.get("running") and not data.get("finished"):
            if time.time() - data.get("updated", 0) > STALE_AFTER or not runstate.pid_alive(data.get("pid")):
                data = {**data, "running": None, "finished": True,
                        "error": data.get("error") or "The run stopped unexpectedly (see runner.err in the state directory)."}
        job_id = data.get("job_id")
        if job_id != self._job_id:   # a different job: start its log from the top
            self._job_id, self._log_offset, s.log = job_id, 0, []
        s.running, s.steps, s.error = data.get("running"), data.get("steps") or {}, data.get("error")
        s.built = bool(data.get("built"))
        s.progress = tuple(data["progress"]) if data.get("progress") else None
        s.new_generation, s.new_kernel = data.get("new_generation"), data.get("new_kernel") or "?"
        s.packages = tuple(data["packages"]) if data.get("packages") else None
        s.build_seconds = data.get("build_seconds") or 0
        if data.get("password_rejected"):
            self._password = None   # ask again next time
        self._read_log()
        if not data.get("finished"):
            self._seen_running.add(job_id)
        elif data.get("offer") and job_id in self._seen_running and job_id not in self._offered:
            # We watched this job run, so the confirm dialog is ours to show (the runner saw an open window).
            self._offered.add(job_id)
            if s.phase() == "ready":
                self._on_offer()

    def _read_log(self):
        try:
            with open(runstate.RUN_LOG, "rb") as f:
                f.seek(self._log_offset)
                chunk = f.read()
        except OSError:
            return
        chunk = chunk[:chunk.rfind(b"\n") + 1]   # only whole lines
        if chunk:
            self._log_offset += len(chunk)
            self.s.log.extend(chunk.decode("utf-8", "replace").splitlines())
