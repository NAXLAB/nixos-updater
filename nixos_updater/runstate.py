"""How the detached runner and the window talk, through plain files in the state directory.

  run.json     the runner's current state (step, progress, error, ...), rewritten ~2x per second
  run.log      the command output, appended line by line
  window.lock  held (flock) by the window for as long as it is open, so the runner can tell if anyone is watching
"""
import fcntl
import json
import os

from .config import STATE_DIR

RUN_JSON = STATE_DIR / "run.json"
RUN_LOG = STATE_DIR / "run.log"
WINDOW_LOCK = STATE_DIR / "window.lock"


def write(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUN_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, RUN_JSON)   # atomic: a reader never sees half a file


def read():
    try:
        return json.loads(RUN_JSON.read_text())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, OverflowError):
        return True
    return True


def hold_window_lock():
    """Called by the window. Keep the returned file object alive; the lock goes away when the process does."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(WINDOW_LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    return f


def window_is_open():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(WINDOW_LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False
