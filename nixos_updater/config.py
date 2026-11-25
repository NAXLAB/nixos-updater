"""Settings, stored as JSON in ~/.config/nixos-updater/config.json.

Only commands you have *edited* are stored (`custom`); everything else is
generated from the working directory, so changing the directory updates them.
"""
import json
import os
import re
from datetime import datetime
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nixos-updater" / "config.json"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "nixos-updater"
# Copy of flake.lock taken at the last successful switch. It is the baseline
# for "which inputs changed" and the source for Roll Back.
LOCK_SNAPSHOT = STATE_DIR / "flake.lock.switched"
# A finished build that hasn't been switched to yet (so a later window can offer the switch).
PENDING_FILE = STATE_DIR / "pending.json"
# Settings fixed by the NixOS module (programs.nixos-updater.*). Same names as the Config fields below.
MANAGED_FILE = Path(os.environ.get("NIXOS_UPDATER_MANAGED", "/etc/nixos-updater/managed.json"))

PERIODS = {"daily": 86400, "weekly": 7 * 86400, "monthly": 30 * 86400}
CHECK_FREQUENCIES = ["daily", "weekly", "monthly"]
GC_FREQUENCIES = ["daily", "weekly", "monthly"]
GC_DAYS = [7, 14, 30, 60, 90]


# The pipeline steps. They have no built-in commands: you enter your own under "Update Pipeline".
COMMAND_KEYS = ("update", "build", "switch", "rollback")
CHECK_TIMES = [f"{h:02d}:00" for h in range(24)]   # on the hour: the timer wakes up hourly
PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


@dataclass
class Config:
    directory: str = str(Path.home() / "nixos-config")
    custom: dict = field(default_factory=dict)   # step -> command ("update", "build", "switch", "rollback")
    auto_update: bool = True                     # run the background check on the schedule below
    check_frequency: str = "weekly"
    check_time: str = "03:00"                    # time of day the background check may start (HH:MM)
    auto_build: bool = True                      # build as soon as an update finds changes
    gc_enabled: bool = False
    gc_frequency: str = "weekly"
    gc_days: int = 30
    last_check: float = 0
    last_gc: float = 0

    # -- settings fixed by the NixOS module ---------------------------------
    managed_keys = frozenset()   # not a dataclass field: filled in by load()

    def is_managed(self, key):
        return key in self.managed_keys

    # -- commands ---------------------------------------------------------
    def command(self, key):
        return self.custom.get(key, "")

    def gc_command(self):
        return f"sudo nix-collect-garbage --delete-older-than {self.gc_days}d"

    # -- schedules --------------------------------------------------------
    def check_due(self, now=None):
        """Due once per period, on or after check_time. Later is fine (laptop was off at 03:00): the next
        timer tick after the time has passed still runs it."""
        days = PERIOD_DAYS.get(self.check_frequency)
        if not self.auto_update or not days:
            return False
        now = now or datetime.now()
        if self.last_check and (now.date() - datetime.fromtimestamp(self.last_check).date()).days < days:
            return False
        return now.strftime("%H:%M") >= self.check_time

    def gc_due(self):
        return self.gc_enabled and time.time() - self.last_gc >= PERIODS[self.gc_frequency]

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls):
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        try:
            managed = json.loads(MANAGED_FILE.read_text())
        except (OSError, ValueError):
            managed = {}
        for key, value in managed.items():   # the module wins over the user's own file
            if key in known:
                setattr(cfg, key, value)
        cfg.managed_keys = frozenset(k for k in managed if k in known)
        if cfg.check_frequency not in CHECK_FREQUENCIES:   # e.g. "manual" from an older version
            cfg.check_frequency = "weekly"
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(cfg.check_time)):
            cfg.check_time = "03:00"
        return cfg

    def save(self):
        """Managed settings are not written back: they come from the module on every load."""
        data = {k: v for k, v in asdict(self).items() if k not in self.managed_keys}
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(data, indent=2))
