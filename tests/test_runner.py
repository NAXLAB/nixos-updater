"""The detached runner and the window-side Remote. python -m unittest discover tests"""
import io, os, signal, subprocess, sys, time, unittest
from pathlib import Path

from test_engine import Base   # also sets the XDG_* env vars before nixos_updater is imported

from nixos_updater import config, runner, runstate  # noqa: E402
from nixos_updater.remote import Remote  # noqa: E402

ROOT = str(Path(__file__).resolve().parent.parent)


def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


class RunnerBase(Base):
    def setUp(self):
        super().setUp()
        runstate.RUN_JSON.unlink(missing_ok=True)
        runstate.RUN_LOG.unlink(missing_ok=True)
        self.opened = []
        self._open_ui, runner.open_ui = runner.open_ui, lambda: self.opened.append(1)
        self._sigterm = signal.getsignal(signal.SIGTERM)
        self._pythonpath = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = ROOT   # for the runner subprocesses started by Remote
        self.window_lock = None

    def tearDown(self):
        runner.open_ui = self._open_ui
        signal.signal(signal.SIGTERM, self._sigterm)
        if self._pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = self._pythonpath
        if self.window_lock:
            self.window_lock.close()

    def run_runner(self, job, stdin="secret\n"):
        return runner.main(self.cfg, job, "t1", stdin=io.StringIO(stdin))

    def open_window(self):
        self.window_lock = runstate.hold_window_lock()

    def finished(self):
        d = runstate.read()
        return bool(d and d["finished"])


class RunnerTests(RunnerBase):
    def test_publishes_state_and_opens_the_window_when_closed(self):
        self.new_lock()
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "mkdir -p result"}
        self.run_runner("check")
        d = runstate.read()
        self.assertTrue(d["finished"] and d["built"] and d["offer"])
        self.assertIsNone(d["error"])
        self.assertIn("Build finished. Nothing has been activated yet.", runstate.RUN_LOG.read_text())
        self.assertEqual(self.opened, [1])

    def test_open_window_shows_the_dialog_itself(self):
        self.open_window()
        self.new_lock()
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "mkdir -p result"}
        self.run_runner("check")
        self.assertEqual(self.opened, [])

    def test_update_only_when_closed_asks_for_the_rebuild(self):
        self.new_lock()
        self.cfg.auto_build = False
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "false"}
        self.run_runner("check")
        self.assertEqual(self.opened, [1])   # the window opens on "Rebuild & Switch?"

    def test_finished_switch_with_window_closed_is_a_notification(self):
        self.cfg.custom = {"switch": "true"}
        self.run_runner("switch")
        self.assertIn("Switch complete", self.notify_rec.read_text())
        self.assertEqual(self.opened, [])

    def test_failure_with_window_closed_notifies_and_click_opens(self):
        self.cfg.custom = {"build": "exit 2"}
        self.run_runner("rebuild")
        self.assertIn("Update failed", self.notify_rec.read_text())
        self.assertEqual(self.opened, [1])   # the fake notify-send "clicks" immediately

    def test_nothing_new_is_quiet(self):
        self.cfg.custom = {"update": "true"}
        self.run_runner("check")
        self.assertEqual(self.opened, [])
        self.assertFalse(self.notify_rec.exists())


class RemoteTests(RunnerBase):
    def remote(self, ask=None, offers=None):
        self.cfg.save()   # the runner process reads the config from disk
        offers = offers if offers is not None else []
        return Remote(self.cfg, ask or (lambda cb: cb("secret")), lambda: offers.append(1))

    def finish(self, r):
        self.assertTrue(wait_for(lambda: (r.poll() or True) and self.finished()), "runner did not finish")
        r.poll()

    def test_job_runs_in_a_detached_process_and_the_window_sees_it(self):
        self.open_window()   # a window is open: it should show the confirm dialog itself
        self.new_lock()
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "echo '[1/2 built] x'; mkdir -p result"}
        offers = []
        r = self.remote(offers=offers)
        r.start("check")
        self.assertEqual(r.s.running, "starting")   # immediately, before the runner is even up
        self.finish(r)
        self.assertEqual(r.s.phase(), "ready")
        self.assertIn("Build finished. Nothing has been activated yet.", r.s.log)
        self.assertEqual(offers, [1])
        self.assertEqual(self.opened, [])

    def test_job_survives_the_window_and_a_new_window_picks_it_up(self):
        self.new_lock()
        self.cfg.custom = {"update": "cp new.lock flake.lock", "build": "sleep 1; mkdir -p result"}
        first = self.remote()
        first.start("check")
        del first   # "close the window" while the job runs (nothing of it is left but its files)
        self.assertTrue(wait_for(lambda: self.finished()), "runner did not finish on its own")
        second = self.remote()   # a window opened later
        self.assertEqual(second.s.phase(), "ready")
        self.assertIn("Build finished. Nothing has been activated yet.", second.s.log)

    def test_cancel(self):
        self.open_window()
        self.cfg.custom = {"build": "sleep 30"}
        r = self.remote()
        r.start("rebuild")
        self.assertTrue(wait_for(lambda: (r.poll() or True) and r.s.steps.get("build") == "running"))
        r.cancel()
        self.finish(r)
        self.assertIsNone(r.s.running)
        self.assertIsNone(r.s.error)
        self.assertIn("Cancelled.", r.s.log)

    def test_sudo_password_round_trip(self):
        self.open_window()
        self.cfg.custom = {"build": "sudo true"}
        r = self.remote()
        r.start("rebuild")
        self.finish(r)
        self.assertIsNone(r.s.error, r.s.log)

    def test_wrong_password_is_forgotten(self):
        self.open_window()
        self.cfg.custom = {"build": "sudo true"}
        r = self.remote(ask=lambda cb: cb("wrong"))
        r.start("rebuild")
        self.finish(r)
        self.assertIsNotNone(r.s.error)
        self.assertIsNone(r._password)

    def test_cancelled_password_dialog_starts_nothing(self):
        self.cfg.custom = {"build": "sudo true"}
        r = self.remote(ask=lambda cb: cb(None))
        r.start("rebuild")
        self.assertIsNone(r.s.running)
        self.assertIsNone(runstate.read())

    def test_dead_runner_is_noticed(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        runstate.write({"job_id": "x", "job": "check", "pid": dead.pid, "finished": False, "running": "build",
                        "steps": {"build": "running"}, "error": None, "built": False, "progress": None,
                        "updated": time.time()})
        r = self.remote()
        self.assertIsNone(r.s.running)
        self.assertIn("stopped unexpectedly", r.s.error)


if __name__ == "__main__":
    unittest.main()
