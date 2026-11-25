"""Tray lifecycle and event-driven UI checks with disposable config and watched folders."""

import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import main
from tray import create_tray_image


class FakeTray:
    # Use the same notification contract as pystray while avoiding extra icons in most tests.
    def __init__(self, notify):
        self.notify = notify
        self.stopped = False
        self.watching = False
        self.enabled = False

    # Report readiness through the actual UI queue, exercising the same hide transition as production.
    def start(self):
        self.notify(("tray_ready",))

    # Store state snapshots so tests can verify menu and GUI synchronization.
    def update_state(self, watching, enabled):
        self.watching, self.enabled = watching, enabled

    # Record clean shutdown without introducing another thread into the fake implementation.
    def stop(self):
        self.stopped = True


class TrayTests(unittest.TestCase):
    # Patch app paths so watching, settings, and history never touch the user's real folders.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        config = self.root / "config.json"
        config.write_text(json.dumps({"categories": {"Documents": [".pdf"], "Others": []}}), encoding="utf-8")
        for change in [patch.object(main, "CONFIG_FILE", config),
                       patch.object(main, "HISTORY_FILE", self.root / "history.json")]:
            change.start()
            self.addCleanup(change.stop)
        self.app = main.DeskSweepApp()
        self.app.withdraw()
        self.app._add_folder(self.inbox, selected=True)

    # Real shutdown is required in cleanup even if a failed assertion leaves watching enabled.
    def tearDown(self):
        if not self.app.executor._shutdown:
            self.app._quit()
            self.app.after(12000, self.app.quit)
            main.tk.Tk.mainloop(self.app)
        self.assertTrue(self.app.executor._shutdown)

    # Run Tk's real event loop; short deadline checks here belong only to the test harness.
    def pump_until(self, predicate, timeout=10):
        deadline, finished = time.monotonic() + timeout, []

        # End the bounded test loop once the expected asynchronous transition has completed.
        def check():
            if predicate():
                finished.append(True)
                self.app.quit()
            elif time.monotonic() >= deadline:
                self.app.quit()
            else:
                self.app.after(20, check)

        self.app.after(20, check)
        main.tk.Tk.mainloop(self.app)
        self.assertTrue(finished, "Timed out waiting for a tray/UI event")

    # Enable watching through the same asynchronous action used by the switch and tray menu.
    def start_watching(self):
        self.app.watch_var.set(True)
        self.app._toggle_watch()
        self.pump_until(lambda: not self.app.busy)
        self.assertIsNotNone(self.app.watcher)

    # Cross-thread tray requests must be handled by Tk without a periodic queue polling timer.
    def send_from_tray(self, name):
        worker = threading.Thread(target=lambda: self.app.events.put((name,)))
        worker.start()
        return worker

    # Closing while watching keeps the same watcher alive and still sorts files while withdrawn.
    def test_close_hides_and_restore_reuses_window(self):
        self.start_watching()
        watcher = self.app.watcher
        with patch.object(main, "TrayController", FakeTray):
            self.app._close()
            self.pump_until(lambda: self.app._hidden_to_tray)
        self.assertEqual(self.app.state(), "withdrawn")
        self.assertTrue(watcher._worker.is_alive())
        (self.inbox / "new.pdf").write_text("download", encoding="utf-8")
        self.pump_until(lambda: (self.inbox / "Documents/new.pdf").exists())
        worker = self.send_from_tray("tray_open")
        self.pump_until(lambda: not self.app._hidden_to_tray)
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertIs(self.app.watcher, watcher)
        self.assertEqual(self.app.state(), "normal")

    # Toggling from the tray changes the real watcher and updates its menu state in both directions.
    def test_tray_toggle_and_explicit_exit(self):
        self.start_watching()
        with patch.object(main, "TrayController", FakeTray):
            self.app._close()
            self.pump_until(lambda: self.app._hidden_to_tray)
        tray = self.app.tray
        worker = self.send_from_tray("tray_toggle")
        self.pump_until(lambda: not self.app.busy and self.app.watcher is None)
        worker.join(timeout=1)
        self.assertFalse(tray.watching)
        self.assertFalse(self.app.watch_var.get())
        worker = self.send_from_tray("tray_toggle")
        self.pump_until(lambda: not self.app.busy and self.app.watcher is not None)
        worker.join(timeout=1)
        watcher = self.app.watcher
        self.assertTrue(tray.watching)
        self.assertTrue(self.app.watch_var.get())
        worker = self.send_from_tray("tray_exit")
        self.app.after(12000, self.app.quit)
        main.tk.Tk.mainloop(self.app)
        worker.join(timeout=1)
        self.assertTrue(self.app.executor._shutdown)
        self.assertTrue(tray.stopped)
        self.assertFalse(watcher._worker.is_alive())
        self.assertFalse(watcher._observer.is_alive())

    # The close button remains a normal quit action when Auto-Watch is off.
    def test_close_with_watch_off_quits(self):
        self.app._close()
        self.app.after(10000, self.app.quit)
        main.tk.Tk.mainloop(self.app)
        self.assertTrue(self.app.executor._shutdown)
        self.assertIsNone(self.app.tray)

    # A tray startup failure leaves the window accessible while its watcher keeps running.
    def test_tray_failure_does_not_hide_window(self):
        self.start_watching()
        with patch.object(main, "TrayController", side_effect=RuntimeError("tray unavailable")):
            self.app._close()
        self.assertFalse(self.app._hidden_to_tray)
        self.assertEqual(self.app.state(), "normal")
        self.assertTrue(self.app.watcher._worker.is_alive())

    # After startup, even an open rules editor should have no recurring Python/Tk after callbacks.
    def test_no_recurring_ui_timers(self):
        self.app._edit_rules()
        self.app.rules_window.withdraw()
        self.app.after(1800, self.app.quit)
        main.tk.Tk.mainloop(self.app)
        self.assertEqual(tuple(self.app.tk.call("after", "info")), ())

    # Check real pystray readiness, menu behavior, and native thread shutdown on Windows.
    def test_real_windows_tray_lifecycle(self):
        self.start_watching()
        watcher = self.app.watcher
        self.app._close()
        self.pump_until(lambda: self.app._hidden_to_tray)
        tray = self.app.tray
        self.assertTrue(tray.icon.visible)
        self.assertTrue(tray._thread.is_alive())
        items = tray.icon.menu.items
        self.assertEqual(items[0].text, "Open DeskSweep")
        self.assertEqual(items[1].text, "Toggle Auto-Watch (On)")
        self.assertEqual(items[-1].text, "Quit")
        self.assertTrue(items[1].checked)
        worker = threading.Thread(target=lambda: items[0](tray.icon))
        worker.start()
        self.pump_until(lambda: not self.app._hidden_to_tray)
        worker.join(timeout=1)
        self.app._quit()
        self.app.after(12000, self.app.quit)
        main.tk.Tk.mainloop(self.app)
        self.assertTrue(self.app.executor._shutdown)
        self.assertFalse(tray._thread.is_alive())
        self.assertFalse(watcher._worker.is_alive())

    # Closing the restored window after toggling watching off must also remove the existing tray icon.
    def test_close_after_disabling_watch_stops_existing_tray(self):
        self.start_watching()
        with patch.object(main, "TrayController", FakeTray):
            self.app._close()
            self.pump_until(lambda: self.app._hidden_to_tray)
        tray = self.app.tray
        self.app._show_window()
        self.app.watch_var.set(False)
        self.app._toggle_watch()
        self.pump_until(lambda: not self.app.busy)
        self.app._close()
        self.app.after(10000, self.app.quit)
        main.tk.Tk.mainloop(self.app)
        self.assertTrue(self.app.executor._shutdown)
        self.assertTrue(tray.stopped)

    # Ensure the in-memory artwork has transparency and can be encoded as an icon without a file.
    def test_in_memory_icon(self):
        image = create_tray_image()
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertIsNotNone(image.getbbox())
        buffer = io.BytesIO()
        image.save(buffer, format="ICO")
        self.assertGreater(len(buffer.getvalue()), 100)
