"""Live watchdog tests, restricted to fresh temporary folders."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from watcher import FolderWatcher


class WatcherTests(unittest.TestCase):
    # Give every watcher its own inbox and journal so real user folders are never involved.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.config = {"categories": {"Documents": [".pdf"], "Others": []}, "ignored_extensions": []}
        self.messages = []
        self.watcher = FolderWatcher([self.inbox], self.config, self.root / "history.json", self.messages.append)
        self.addCleanup(self.watcher.stop)

    # Poll with a deadline so slow event delivery is tolerated without hanging the test suite.
    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(f"Watcher timed out. Messages: {self.messages}")

    # Watch a completed download rename while leaving pre-existing and temporary files untouched.
    def test_renamed_download_and_clean_stop(self):
        existing = self.inbox / "existing.pdf"
        existing.write_text("old", encoding="utf-8")
        self.watcher.start()
        temporary = self.inbox / "lecture.pdf.crdownload"
        temporary.write_text("download", encoding="utf-8")
        time.sleep(2.3)
        self.assertTrue(temporary.exists())
        source = temporary.with_suffix("")
        temporary.rename(source)
        self.wait_for(lambda: (self.inbox / "Documents/lecture.pdf").exists())
        self.assertTrue(existing.exists())
        self.assertFalse(source.exists())
        self.watcher.stop()
        self.assertFalse(self.watcher._observer.is_alive())
        self.assertFalse(self.watcher._worker.is_alive())
        later = self.inbox / "later.pdf"
        later.write_text("after stop", encoding="utf-8")
        time.sleep(2.2)
        self.assertTrue(later.exists())

    # Continual writes must postpone sorting until the configured quiet period has elapsed.
    def test_waits_for_quiet_and_ignores_subfolders(self):
        nested = self.inbox / "existing"
        nested.mkdir()
        self.watcher.start()
        nested_file = nested / "nested.pdf"
        nested_file.write_text("leave me", encoding="utf-8")
        source = self.inbox / "stream.pdf"
        for i in range(5):
            source.write_text("x" * (i + 1), encoding="utf-8")
            time.sleep(0.6)
            self.assertTrue(source.exists(), "An active download moved too early")
        time.sleep(0.6)
        self.assertTrue(source.exists())
        self.wait_for(lambda: (self.inbox / "Documents/stream.pdf").exists())
        self.assertTrue(nested_file.exists())
        self.assertEqual((self.inbox / "Documents/stream.pdf").read_text(), "xxxxx")

    # Reject overlapping watches to prevent a move into a category from triggering an endless loop.
    def test_overlapping_watches_rejected(self):
        child = self.inbox / "Documents"
        child.mkdir()
        watcher = FolderWatcher([self.inbox, child], self.config, self.root / "history.json")
        try:
            with self.assertRaises(ValueError):
                watcher.start()
        finally:
            watcher.stop()

    # An idle sorter must wait indefinitely until a filesystem notification or shutdown wakes it.
    def test_idle_worker_has_no_periodic_wakeups(self):
        entered = threading.Event()
        original_wait = self.watcher._changed.wait

        # Observe the real blocking wait without replacing its synchronization behavior.
        def observed_wait(timeout=None):
            entered.set()
            return original_wait(timeout)

        with patch.object(self.watcher._changed, "wait", side_effect=observed_wait) as waiting:
            self.watcher.start()
            self.assertTrue(entered.wait(timeout=2))
            time.sleep(0.5)
            self.assertEqual(waiting.call_count, 1)
            self.assertEqual(waiting.call_args.args, ())
            self.assertEqual(waiting.call_args.kwargs, {})
            self.watcher.stop()

    # Sort into a custom destination without processing arrivals inside that destination again.
    def test_custom_destination_does_not_loop(self):
        output = self.root / "sorted"
        category = output / "Documents"
        category.mkdir(parents=True)
        config = dict(self.config, destination_root=str(output))
        watcher = FolderWatcher([self.inbox, category], config, self.root / "history.json", self.messages.append)
        try:
            watcher.start()
            source = self.inbox / "new.pdf"
            source.write_text("new arrival", encoding="utf-8")
            target = category / source.name
            self.wait_for(target.exists)
            time.sleep(2.4)
            self.assertTrue(target.exists())
            self.assertFalse(source.exists())
            self.assertFalse((category / "Documents").exists())
            with watcher._lock:
                self.assertNotIn(target, watcher._pending)
        finally:
            watcher.stop()

    # Config values and legacy explicit overrides share validation without a hardcoded minimum delay.
    def test_configured_delay_and_override(self):
        watcher = FolderWatcher([self.inbox], dict(self.config, settle_seconds=0.25))
        self.assertEqual(watcher.settle_seconds, 0.25)
        overridden = FolderWatcher([self.inbox], self.config, settle_seconds=0.5)
        self.assertEqual(overridden.settle_seconds, 0.5)
        for seconds in [0, -1, True, float("inf"), "3"]:
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    FolderWatcher([self.inbox], self.config, settle_seconds=seconds)

    # Changing the delay re-times a queued file and wakes the sleeping sorter without replacing threads.
    def test_live_delay_change_reschedules_pending_arrival(self):
        self.watcher.set_settle_seconds(60)
        source = self.inbox / "quiet.pdf"
        source.write_text("test", encoding="utf-8")
        self.watcher.enqueue(source)
        with self.watcher._lock:
            initial_due = self.watcher._pending[source]["due"]
        self.watcher.set_settle_seconds(120)
        with self.watcher._lock:
            self.assertAlmostEqual(self.watcher._pending[source]["due"], initial_due + 60)
        with self.assertRaises(ValueError):
            self.watcher.set_settle_seconds(0)
        self.assertEqual(self.watcher.settle_seconds, 120)
        self.watcher.start()
        self.watcher.set_settle_seconds(0.1)
        self.wait_for(lambda: (self.inbox / "Documents/quiet.pdf").exists(), timeout=3)


if __name__ == "__main__":
    unittest.main()
