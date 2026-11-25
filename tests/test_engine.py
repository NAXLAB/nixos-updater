"""GTK-free tests: python -m unittest discover tests"""
import json, os, stat, sys, tempfile, time, unittest
from datetime import datetime, timedelta
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ["XDG_STATE_HOME"] = TMP + "/state"
os.environ["XDG_CONFIG_HOME"] = TMP + "/config"
os.environ["NIXOS_UPDATER_MANAGED"] = TMP + "/managed.json"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nixos_updater import background, config, engine  # noqa: E402


def lock(revs):
    nodes = {"root": {"inputs": {n: n for n in revs}}}
    nodes.update({n: {"locked": {"rev": r}} for n, r in revs.items()})
    return json.dumps({"nodes": nodes, "root": "root", "version": 7})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "flake.lock").write_text(lock({"nixpkgs": "aaa", "home": "bbb"}))
        config.LOCK_SNAPSHOT.unlink(missing_ok=True)
        config.PENDING_FILE.unlink(missing_ok=True)
        config.MANAGED_FILE.unlink(missing_ok=True)
        # fake sudo: checks the password on stdin, then runs the command
        bin_dir = Path(tempfile.mkdtemp())
        fake = bin_dir / "sudo"
        fake.write_text('#!/bin/sh\nshift; shift; shift\nread pw\n[ "$pw" = secret ] || { echo "Sorry, try again." >&2; exit 1; }\nexec "$@"\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        # fake notify-send: records its arguments, then behaves as if the notification was clicked
        self.notify_rec = bin_dir / "notify.txt"
        notify = bin_dir / "notify-send"
        notify.write_text(f'#!/bin/sh\necho "$@" > {self.notify_rec}\necho default\n')
        notify.chmod(notify.stat().st_mode | stat.S_IEXEC)
        os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        self.cfg = config.Config(directory=str(self.dir))
        self.asked = 0
        self.offered = 0
        self.pw = "secret"
        self.eng = engine.Engine(self.cfg, lambda: None, self._ask, self._offer)

    def _ask(self):
        self.asked += 1
        return self.pw

    def _offer(self):
        self.offered += 1

    def new_lock(self, nixpkgs="ccc"):
        (self.dir / "new.lock").write_text(lock({"nixpkgs": nixpkgs, "home": "bbb"}))

    def run_job(self, job):
        self.eng.start(job)
        for _ in range(200):
            if not self.eng.s.running:
                return
            time.sleep(0.05)
        self.fail("job did not finish")


class Tests(Base):
    def test_first_run_snapshot_no_changes(self):
        self.assertTrue(config.LOCK_SNAPSHOT.exists())
        self.assertEqual(self.eng.s.phase(), "uptodate")
        self.assertFalse(self.eng.s.rollback_available)

    def test_check_builds_offers_switches(self):
        (self.dir / "new.lock").write_text(lock({"nixpkgs": "ccc", "home": "bbb"}))
        self.cfg.custom = {"update": "sudo cp new.lock flake.lock",
                           "build": "echo '[3/10 built]'; mkdir -p result",
                           "switch": "sudo true"}
        self.run_job("check")
        s = self.eng.s
        self.assertIsNone(s.error, s.log)
        self.assertEqual(s.phase(), "ready")
        self.assertEqual(self.offered, 1)
        self.assertEqual(s.progress, (3, 10))
        self.assertEqual([i.name for i in s.inputs if i.changed], ["nixpkgs"])
        self.assertTrue(s.rollback_available)
        self.assertEqual(self.asked, 1)
        self.run_job("switch")
        self.assertIsNone(s.error, s.log)
        self.assertEqual(s.phase(), "uptodate")
        self.assertFalse(s.rollback_available)
        self.assertEqual(self.asked, 1)   # password cached

    def test_no_changes_skips_build(self):
        self.cfg.custom = {"update": "true", "build": "false"}
        self.run_job("check")
        self.assertEqual(self.eng.s.phase(), "uptodate")
        self.assertEqual(self.offered, 0)

    def test_build_failure_then_rollback(self):
        (self.dir / "new.lock").write_text(lock({"nixpkgs": "zzz", "home": "bbb"}))
        self.cfg.custom = {"update": "cp new.lock flake.lock",
                           "build": "echo boom; exit 3",
                           "rollback": f"cp {config.LOCK_SNAPSHOT} flake.lock"}
        self.run_job("check")
        s = self.eng.s
        self.assertEqual(s.phase(), "error")
        self.assertIn("exit status 3", s.error)
        self.assertEqual(s.steps["build"], "failed")
        self.assertTrue(s.rollback_available)
        self.run_job("rollback")
        self.assertEqual(s.phase(), "uptodate")

    def test_wrong_password_cleared(self):
        self.pw = "nope"
        self.cfg.custom = {"build": "sudo true"}
        self.run_job("rebuild")
        self.assertEqual(self.eng.s.phase(), "error")
        self.assertIsNone(self.eng._password)

    def test_password_dialog_cancel(self):
        self.pw = None
        self.cfg.custom = {"build": "sudo true"}
        self.run_job("rebuild")
        self.assertIsNone(self.eng.s.error)
        self.assertEqual(self.eng.s.steps, {"build": "idle"})

    def test_cancel_running(self):
        self.cfg.custom = {"build": "sleep 30"}
        self.eng.start("rebuild")
        time.sleep(0.5)
        self.eng.cancel()
        for _ in range(60):
            if not self.eng.s.running:
                break
            time.sleep(0.1)
        self.assertIsNone(self.eng.s.running)
        self.assertIsNone(self.eng.s.error)

    def test_cancel_sudo_command(self):
        # the sudo'd command must also die (via `sudo pkill -s`), not just our bash
        self.cfg.custom = {"build": "sudo sleep 30"}
        self.eng.start("rebuild")
        time.sleep(0.7)
        self.eng.cancel()
        for _ in range(100):
            if not self.eng.s.running:
                break
            time.sleep(0.1)
        self.assertIsNone(self.eng.s.running)
        self.assertIn("Cancelled.", self.eng.s.log)

    def test_auto_build_off_stops_after_update(self):
        self.new_lock()
        self.cfg.auto_build = False
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "false"}
        self.run_job("check")
        s = self.eng.s
        self.assertIsNone(s.error, s.log)
        self.assertEqual(s.phase(), "updates")
        self.assertNotIn("build", s.steps)
        self.assertEqual(self.offered, 0)

    def test_pending_build_survives_a_new_engine(self):
        self.new_lock()
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "mkdir -p result"}
        self.run_job("check")
        self.assertEqual(self.eng.s.phase(), "ready")
        again = engine.Engine(self.cfg, lambda: None, self._ask, self._offer)   # e.g. the window opened later
        self.assertEqual(again.s.phase(), "ready")
        self.assertEqual(again.s.new_generation, self.eng.s.new_generation)
        (self.dir / "flake.lock").write_text(lock({"nixpkgs": "other", "home": "bbb"}))   # lock moved on
        self.assertEqual(engine.Engine(self.cfg, lambda: None, self._ask, self._offer).s.phase(), "updates")

    def test_check_does_not_rebuild_a_finished_build(self):
        self.new_lock()
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "mkdir -p result"}
        self.run_job("check")
        self.cfg.custom["update"] = "true"
        self.cfg.custom["build"] = "false"      # would fail if it ran again
        self.run_job("check")
        self.assertIsNone(self.eng.s.error, self.eng.s.log)
        self.assertEqual(self.eng.s.phase(), "ready")
        self.assertEqual(self.offered, 2)

    def test_headless_has_no_password_and_no_sudo(self):
        self.new_lock()
        self.cfg.custom = {"update": "sudo cp new.lock flake.lock", "build": "mkdir -p result"}
        eng = engine.Engine(self.cfg, lambda: None, self._ask, self._offer, headless=True)
        eng.start("check")
        eng.thread.join()
        self.assertIsNone(eng.s.error, eng.s.log)
        self.assertEqual(self.asked, 0)
        self.assertEqual(eng.s.phase(), "ready")

    def test_second_run_is_refused_while_one_is_running(self):
        self.cfg.custom = {"build": "sleep 2"}
        self.eng.start("rebuild")
        time.sleep(0.4)
        other = engine.Engine(self.cfg, lambda: None, self._ask, self._offer, headless=True)
        other.start("check")
        other.thread.join()
        self.assertTrue(other.busy)
        self.eng.cancel()
        self.eng.thread.join(5)

    def test_background_notifies_and_click_opens_ui(self):
        self.new_lock()
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "mkdir -p result"}
        launched = []
        background.run(self.cfg, launch=lambda: launched.append(1))
        self.assertIn("Ready to switch", self.notify_rec.read_text())
        self.assertEqual(launched, [1])
        self.assertGreater(self.cfg.last_check, 0)

    def test_background_without_auto_build_asks_for_a_build(self):
        self.new_lock()
        self.cfg.auto_build = False
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "false"}
        background.run(self.cfg, launch=lambda: None)
        self.assertIn("Flake updated", self.notify_rec.read_text())

    def test_background_up_to_date_is_silent(self):
        self.cfg.custom = {"update": "true"}
        background.run(self.cfg, launch=lambda: self.fail("should not open"))
        self.assertFalse(self.notify_rec.exists())

    def test_background_failure_is_reported(self):
        self.new_lock()
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "exit 1"}
        background.run(self.cfg, launch=lambda: None)
        self.assertIn("Update failed", self.notify_rec.read_text())

    def test_module_settings_win_and_are_not_saved(self):
        config.MANAGED_FILE.write_text(json.dumps({"auto_build": False, "check_frequency": "daily", "bogus": 1}))
        c = config.Config.load()
        self.assertFalse(c.auto_build)
        self.assertEqual(c.check_frequency, "daily")
        self.assertEqual(c.managed_keys, {"auto_build", "check_frequency"})
        self.assertTrue(c.is_managed("auto_build"))
        self.assertFalse(c.is_managed("directory"))
        c.auto_build = True
        c.save()
        self.assertNotIn("auto_build", json.loads(config.CONFIG_FILE.read_text()))

    def test_boot_mode_rewrites_command(self):
        self.cfg.custom = {"switch": "echo nixos-rebuild switch --flake x"}
        self.run_job("switch_boot")
        self.assertIn("$ echo nixos-rebuild boot --flake x", self.eng.s.log)

    def test_config_roundtrip_and_defaults(self):
        self.cfg.custom = {"update": "x"}
        self.cfg.save()
        c = config.Config.load()
        self.assertEqual(c.command("update"), "x")
        self.assertEqual(c.command("build"), "")   # no built-in commands
        noon = datetime(2026, 11, 24, 12, 0)
        self.assertTrue(config.Config(check_frequency="monthly").check_due(noon))
        self.assertFalse(config.Config(check_frequency="monthly", auto_update=False).check_due(noon))
        self.assertFalse(config.Config(check_frequency="weekly", last_check=time.time()).check_due())

    def test_check_due_respects_time_of_day(self):
        day = datetime(2026, 11, 24)
        yesterday = (day - timedelta(days=1)).replace(hour=3).timestamp()
        cfg = config.Config(check_frequency="daily", check_time="13:00", last_check=yesterday)
        self.assertFalse(cfg.check_due(day.replace(hour=12, minute=59)))
        self.assertTrue(cfg.check_due(day.replace(hour=13)))
        self.assertTrue(cfg.check_due(day.replace(hour=22)))     # late is fine (laptop was off)
        cfg.last_check = day.replace(hour=13, minute=5).timestamp()
        self.assertFalse(cfg.check_due(day.replace(hour=22)))    # already ran today
        weekly = config.Config(check_frequency="weekly", check_time="03:00",
                               last_check=(day - timedelta(days=5)).timestamp())
        self.assertFalse(weekly.check_due(day.replace(hour=12)))
        weekly.last_check = (day - timedelta(days=7)).timestamp()
        self.assertTrue(weekly.check_due(day.replace(hour=12)))

    def test_bad_time_in_config_falls_back(self):
        config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_FILE.write_text(json.dumps({"check_time": "25:99"}))
        self.assertEqual(config.Config.load().check_time, "03:00")

    def test_blank_command_is_a_clear_error(self):
        self.run_job("rebuild")   # no commands configured
        s = self.eng.s
        self.assertEqual(s.phase(), "error")
        self.assertIn("No build command set", s.error)
        self.assertEqual(s.steps["build"], "failed")


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    unittest.main()
