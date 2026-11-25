"""Everything that isn't GTK: reading system/flake state and running the pipeline.

The window owns an Engine, calls engine.start("check"), and re-reads engine.s
(a State) whenever `on_change` fires. Jobs run on a worker thread.

Jobs:  check            update -> (build if anything changed) -> offer switch
       rebuild          build
       rebuild_switch   build -> offer switch
       switch / switch_boot / rollback
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import LOCK_SNAPSHOT, PENDING_FILE, STATE_DIR

CURRENT = Path("/run/current-system")
BOOTED = Path("/run/booted-system")
SYSTEM_PROFILE = Path("/nix/var/nix/profiles/system")

# Commands run through bash with `sudo` replaced by a function that reads the
# password from stdin (there is no terminal to ask on).
SUDO_SHIM = 'sudo() { command sudo -S -p "" "$@"; }\n'
# Unattended runs have nobody to ask for a password, so `sudo` just runs the command as the current user.
# (Update and build don't need root; only the switch does, and that always happens in the window.)
HEADLESS_SHIM = 'sudo() { "$@"; }\n'
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BUILD_PROGRESS = re.compile(r"\[(\d+)/(\d+) built")

STEP_LABELS = {
    "update": "Updating flake inputs", "build": "Building", "switch": "Switching",
    "rollback": "Rolling back", "gc": "Collecting garbage",
}


class Cancelled(Exception):
    pass


class StepFailed(Exception):
    pass


# --- reading system state ---------------------------------------------------

def read_lock(path):
    """flake.lock -> {input name: locked rev}. Only direct inputs of the root."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    nodes = data.get("nodes", {})
    out = {}
    for name, key in nodes.get(data.get("root", "root"), {}).get("inputs", {}).items():
        if isinstance(key, str):  # lists are `follows` references
            locked = nodes.get(key, {}).get("locked", {})
            out[name] = locked.get("rev") or locked.get("narHash") or "?"
    return out


def kernel_version(system):
    m = re.search(r"linux-([\d.]+)", os.path.realpath(Path(system) / "kernel"))
    return m.group(1) if m else "?"


def current_generation():
    try:
        return int(re.search(r"system-(\d+)-link", os.readlink(SYSTEM_PROFILE)).group(1))
    except (OSError, AttributeError):
        return None


def diff_closures(old, new):
    """(changed, added, removed) package counts from `nix store diff-closures`, or None."""
    try:
        r = subprocess.run(
            ["nix", "--extra-experimental-features", "nix-command flakes",
             "store", "diff-closures", str(old), str(new)],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    changed = added = removed = 0
    for line in r.stdout.splitlines():
        if ": " not in line or "→" not in line:
            continue
        left, right = line.split(": ", 1)[1].split("→", 1)
        if left.strip() == "∅":
            added += 1
        elif right.strip().startswith("∅"):
            removed += 1
        else:
            changed += 1
    return changed, added, removed


# --- state shown by the UI --------------------------------------------------

@dataclass
class FlakeInput:
    name: str
    old: str   # rev at last switch ("" = new input)
    new: str   # rev now

    @property
    def changed(self):
        return self.old != self.new


@dataclass
class State:
    running: str | None = None            # step key currently running
    steps: dict = field(default_factory=dict)   # key -> idle|running|done|failed
    error: str | None = None
    built: bool = False                   # a build is waiting to be switched to
    inputs: list = field(default_factory=list)
    rollback_available: bool = False
    generation: int | None = None
    kernel: str = "?"
    restart: bool = False                 # running kernel != booted kernel
    new_generation: int | None = None
    new_kernel: str = "?"
    packages: tuple | None = None         # (changed, added, removed)
    build_seconds: float = 0
    progress: tuple | None = None         # (done, total) derivations
    log: list = field(default_factory=list)

    def phase(self):
        if self.running:
            return "running"
        if self.error:
            return "error"
        if self.built:
            return "ready"
        if any(i.changed for i in self.inputs):
            return "updates"
        if self.restart:
            return "restart"
        return "uptodate"


# --- the pipeline -----------------------------------------------------------

class Engine:
    def __init__(self, cfg, on_change, ask_password, offer_switch, headless=False):
        """on_change(): state changed (any thread). ask_password(): blocking, returns str or
        None. offer_switch(): a build is ready, ask the user (any thread).
        headless: no window and no password; see HEADLESS_SHIM."""
        self.cfg, self.notify, self.headless = cfg, on_change, headless
        self._ask_password, self.offer_switch = ask_password, offer_switch
        self.s = State()
        self.thread = None
        self.busy = False            # last start() was refused: another run holds the run lock
        self.password_rejected = False   # sudo said the password was wrong (the window forgets its copy)
        self._built_lock = None      # hash of the flake.lock the waiting build was made from
        self._password = None
        self._proc = None
        self._cancel = False
        self.refresh()
        self._load_pending()

    # -- state ------------------------------------------------------------
    def refresh(self):
        s = self.s
        lock = Path(self.cfg.directory) / "flake.lock"
        if lock.exists() and not LOCK_SNAPSHOT.exists():   # first run: assume lock == running system
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy(lock, LOCK_SNAPSHOT)
        old, new = read_lock(LOCK_SNAPSHOT), read_lock(lock)
        s.inputs = [FlakeInput(n, old.get(n, ""), new[n]) for n in sorted(new)]
        try:
            s.rollback_available = lock.read_bytes() != LOCK_SNAPSHOT.read_bytes()
        except OSError:   # no lock or no snapshot
            s.rollback_available = False
        s.generation = current_generation()
        s.kernel = kernel_version(CURRENT)
        s.restart = s.kernel != "?" and s.kernel != kernel_version(BOOTED)

    # -- control ----------------------------------------------------------
    def start(self, job):
        if self.s.running:
            return
        self.s.running = "starting"   # set here so a double click can't start two jobs
        self.notify()
        self.thread = threading.Thread(target=self._job, args=(job,), daemon=True)
        self.thread.start()

    def cancel(self):
        self._cancel = True
        self.s.log.append("Cancelling…")
        self.notify()
        proc = self._proc
        if proc and proc.poll() is None:
            threading.Thread(target=self._kill, args=(proc,), daemon=True).start()

    def _kill(self, proc):
        """Stop the command's whole session. Commands started through sudo run as root, which our own
        signals can't reach, so each round also asks sudo to `pkill` the session. TERM first, KILL if it lingers."""
        sid = proc.pid   # start_new_session=True made bash the leader of its own session
        for sig in ("TERM", "KILL"):
            try:
                os.killpg(sid, getattr(signal, "SIG" + sig))
            except OSError:
                pass
            if self._password:
                try:
                    subprocess.run(["sudo", "-S", "-p", "", "pkill", f"-{sig}", "-s", str(sid)],
                                   input=self._password + "\n", text=True, capture_output=True, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    pass
            for _ in range(30):   # wait up to 3 s before escalating
                if proc.poll() is not None:
                    return
                time.sleep(0.1)

    # -- jobs -------------------------------------------------------------
    def _job(self, job):
        s = self.s
        s.error, s.steps, s.log, s.progress = None, {}, [], None
        self._cancel = False
        self.busy = False
        self.password_rejected = False
        offer = False
        t0 = time.time()
        run_lock = self._acquire_run_lock()
        if run_lock is None:    # the window and the background run must never work at the same time
            self.busy = True
            s.error = "Another NixOS Updater run is in progress."
            s.running = None
            self.notify()
            return
        try:
            if job == "rollback":
                self._step("rollback")
                self._clear_pending()
                s.log.append("flake.lock restored.")
            elif job.startswith("switch"):
                self._step("switch", boot=(job == "switch_boot"))
                s.log.append("Switch complete.")
                self._after_switch()
            else:
                if job == "check":
                    self._step("update")
                    self.cfg.last_check = time.time()
                    self.cfg.save()
                    self.refresh()
                    if not self._built_for_current_lock():
                        s.built = False          # the lock moved on: an older build is stale
                changed = any(i.changed for i in s.inputs)
                if job == "check" and self._built_for_current_lock():
                    offer = True                 # nothing new, but a finished build is still waiting
                elif job != "check" or (changed and self.cfg.auto_build):
                    self._step("build")
                    s.built = True
                    s.log.append("Build finished. Nothing has been activated yet.")
                    s.build_seconds = time.time() - t0
                    self._summarize_build()
                    self._save_pending()
                    offer = job in ("check", "rebuild_switch")
        except Cancelled:
            s.log.append("Cancelled.")
        except StepFailed as e:
            s.error = str(e)
            s.log.append(f"Error: {e}")
        finally:
            run_lock.close()   # before clearing `running`, so a run started right after never finds it still held
            s.running = None
            self.refresh()
            self.notify()
        if offer:
            self.offer_switch()

    def _acquire_run_lock(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        f = open(STATE_DIR / "run.lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            f.close()
            return None

    # -- a finished build waiting for its switch (survives closing the window) --
    def _lock_hash(self):
        try:
            return hashlib.sha256((Path(self.cfg.directory) / "flake.lock").read_bytes()).hexdigest()
        except OSError:
            return ""

    def _built_for_current_lock(self):
        return self.s.built and self._built_lock == self._lock_hash()

    def _save_pending(self):
        s = self.s
        self._built_lock = self._lock_hash()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_FILE.write_text(json.dumps({
            "lock": self._built_lock, "generation": s.new_generation, "kernel": s.new_kernel,
            "packages": s.packages, "seconds": s.build_seconds}))

    def _clear_pending(self):
        self._built_lock = None
        self.s.built = False
        PENDING_FILE.unlink(missing_ok=True)

    def _load_pending(self):
        """Pick up a build from an earlier run, if flake.lock hasn't changed since and ./result is still there."""
        try:
            d = json.loads(PENDING_FILE.read_text())
        except (OSError, ValueError):
            return
        if d.get("lock") != self._lock_hash() or not (Path(self.cfg.directory) / "result").exists():
            return
        s = self.s
        s.built, self._built_lock = True, d["lock"]
        s.new_generation, s.new_kernel = d.get("generation"), d.get("kernel", "?")
        s.packages = tuple(d["packages"]) if d.get("packages") else None
        s.build_seconds = d.get("seconds", 0)

    def _after_switch(self):
        s = self.s
        shutil.copy(Path(self.cfg.directory) / "flake.lock", LOCK_SNAPSHOT)
        self._clear_pending()
        if self.cfg.gc_due():
            try:
                self._step("gc")
                self.cfg.last_gc = time.time()
                self.cfg.save()
            except StepFailed:
                s.log.append("Garbage collection failed (the switch itself succeeded).")

    def _summarize_build(self):
        s = self.s
        result = Path(self.cfg.directory) / "result"
        s.new_generation = (s.generation or 0) + 1
        s.new_kernel = kernel_version(result)
        s.packages = diff_closures(CURRENT, result)

    # -- running one command ---------------------------------------------
    def _step(self, key, boot=False):
        s = self.s
        if self._cancel:   # cancelled between steps (or while the password dialog was open)
            raise Cancelled
        cmd = self.cfg.gc_command() if key == "gc" else self.cfg.command(key)
        if boot:
            cmd = cmd.replace("nixos-rebuild switch", "nixos-rebuild boot")
        if not cmd.strip():
            s.steps[key] = "failed"
            raise StepFailed(f"No {key} command set. Enter one under Update Pipeline")
        s.running, s.steps[key] = key, "running"
        s.log.append(f"$ {cmd}")
        self.notify()
        try:
            rc = self._run(cmd)
        except Cancelled:   # password dialog dismissed
            s.steps[key] = "idle"
            raise
        if self._cancel:
            s.steps[key] = "idle"
            raise Cancelled
        if rc != 0:
            s.steps[key] = "failed"
            raise StepFailed(f"{STEP_LABELS[key]} failed (exit status {rc})")
        s.steps[key] = "done"

    def _run(self, cmd):
        s = self.s
        wants_sudo = "sudo" in cmd and not self.headless
        if wants_sudo and self._password is None:
            self._password = self._ask_password()
            if self._password is None or self._cancel:
                raise Cancelled
        proc = self._proc = subprocess.Popen(
            ["bash", "-c", (HEADLESS_SHIM if self.headless else SUDO_SHIM) + cmd], cwd=self.cfg.directory,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=True)  # own process group, so Cancel can kill it all
        try:
            if wants_sudo:  # one copy of the password per possible sudo call
                proc.stdin.write((self._password + "\n") * cmd.count("sudo"))
            proc.stdin.close()
        except BrokenPipeError:
            pass
        for line in proc.stdout:
            line = ANSI.sub("", line).rstrip()
            if not line:
                continue
            if "incorrect password" in line or "Sorry, try again" in line:
                self._password = None   # ask again next time
                self.password_rejected = True
            if m := BUILD_PROGRESS.search(line):
                s.progress = (int(m.group(1)), int(m.group(2)))
            s.log.append(line)
            self.notify()
        return proc.wait()
