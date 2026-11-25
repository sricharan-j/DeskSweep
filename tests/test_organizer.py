"""Safety regression tests; all file operations stay inside disposable test folders."""

import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import organizer


class OrganizerTests(unittest.TestCase):
    # Isolate every test from real Downloads, Desktop, and the application's own history.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "inbox"
        self.folder.mkdir()
        self.history = self.root / "history.json"
        self.config = {"categories": {"Documents": [".pdf", ".txt"], "Code": [".py"],
                                      "Images": [".png"], "Others": []}, "ignored_extensions": []}

    # Create a tiny fixture with predictable contents so byte preservation can be asserted.
    def write(self, name, content="original", folder=None):
        path = (folder or self.folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    # Use the public planning API to exercise the same workflow as the GUI.
    def scan(self):
        return organizer.scan_folders([self.folder], self.config)

    # Older configs receive the new default, while positive fractional and integer delays persist.
    def test_settle_seconds_default_and_round_trip(self):
        self.assertEqual(organizer.validate_config(self.config)["settle_seconds"], 3.0)
        config_file = self.root / "config.json"
        for seconds in [0.25, 1, 3.0, 60]:
            with self.subTest(seconds=seconds):
                config = organizer.validate_config(dict(self.config, settle_seconds=seconds))
                organizer.atomic_write_json(config_file, config)
                self.assertEqual(organizer.load_config(config_file)["settle_seconds"], seconds)

    # Reject nonnumbers, booleans, and nonfinite values before they can reach the worker's timed wait.
    def test_invalid_settle_seconds_rejected(self):
        for seconds in [0, -1, True, False, None, "3", [], {}, float("nan"), float("inf"), -float("inf"), 10**400]:
            with self.subTest(seconds=seconds):
                with self.assertRaisesRegex(ValueError, "settle_seconds"):
                    organizer.validate_config(dict(self.config, settle_seconds=seconds))

    # Keep both old configs and an explicit empty destination compatible with in-place sorting.
    def test_default_destination_routing(self):
        source = self.write("lecture.pdf")
        for value in [None, ""]:
            if value is not None:
                self.config["destination_root"] = value
            with self.subTest(destination=value):
                plan = self.scan()
                self.assertEqual(plan[0]["target_path"], str(self.folder / "Documents/lecture.pdf"))
                self.assertTrue(source.exists())
                self.assertFalse((self.folder / "Documents").exists())

    # Route into the selected output and retain enough history to undo without consulting current rules.
    def test_custom_destination_round_trip(self):
        output = self.root / "sorted"
        output.mkdir()
        source = self.write("lecture.pdf")
        self.config["destination_root"] = str(output)
        plan = self.scan()
        self.assertEqual(plan[0]["target_path"], str(output / "Documents/lecture.pdf"))
        self.assertEqual(plan[0]["destination_root"], str(output))
        self.assertFalse((output / "Documents").exists())
        result = organizer.execute_moves(plan, self.history)
        self.assertEqual(result["errors"], [])
        self.assertEqual((output / "Documents/lecture.pdf").read_text(), "original")
        self.assertFalse((self.folder / "Documents").exists())
        self.config["destination_root"] = ""
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(source)])
        self.assertTrue(output.exists())
        self.assertFalse((output / "Documents").exists())

    # Reserve duplicate names across multiple source folders sharing one custom destination.
    def test_custom_destination_collisions_across_sources(self):
        output = self.root / "sorted"
        first = self.write("report.pdf", "first")
        second = self.write("report.pdf", "second", self.root / "second inbox")
        existing = self.write("report.pdf", "existing", output / "Documents")
        self.config["destination_root"] = str(output)
        plan = organizer.scan_folders([first.parent, second.parent], self.config)
        self.assertEqual([Path(p["target_path"]).name for p in plan], ["report (1).pdf", "report (2).pdf"])
        self.assertEqual(len(organizer.execute_moves(plan, self.history)["moved"]), 2)
        self.assertEqual(existing.read_text(), "existing")
        self.assertEqual(len(organizer.undo_last_batch(self.history)["restored"]), 2)
        self.assertEqual(first.read_text(), "first")
        self.assertEqual(second.read_text(), "second")
        self.assertTrue(existing.exists())

    # Skip the entire output tree, including its root, without confusing similarly named siblings.
    def test_destination_tree_is_excluded(self):
        output = self.root / "sorted"
        self.write("root.pdf", folder=output)
        self.write("nested.pdf", folder=output / "Documents")
        sibling = self.write("allowed.pdf", folder=self.root / "sorted-input")
        self.config["destination_root"] = str(output)
        plan = organizer.scan_folders([output, output / "Documents", sibling.parent], self.config)
        self.assertEqual([p["source_path"] for p in plan], [str(sibling)])
        self.config["destination_root"] = str(self.folder)
        self.write("same-root.pdf")
        self.assertEqual(self.scan(), [])

    # Reject unavailable or malformed destinations so a typo cannot silently sort beside the source.
    def test_invalid_destination(self):
        source = self.write("lecture.pdf")
        for value in [str(self.root / "missing"), str(source), "relative/path", 42]:
            with self.subTest(destination=value), self.assertRaises((OSError, ValueError)):
                organizer.scan_folders([self.folder], dict(self.config, destination_root=value))
        self.assertTrue(source.exists())
        self.assertFalse((self.folder / "Documents").exists())

    # Check the recorded root at execution so a modified plan cannot redirect an approved output.
    def test_custom_destination_tampered_plan(self):
        output = self.root / "sorted"
        output.mkdir()
        source = self.write("lecture.pdf")
        self.config["destination_root"] = str(output)
        plan = self.scan()
        plan[0]["target_path"] = str(self.root / "elsewhere/Documents/lecture.pdf")
        result = organizer.execute_moves(plan, self.history)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(source.exists())

    # Model two drives while allowing the staged file's final rename on its destination drive.
    def cross_drive_move(self, source, target):
        if source.parent != target.parent:
            raise OSError(errno.EXDEV, "simulated cross-drive move")
        self.real_move(source, target)

    # Preserve bytes and exact paths when organizing and undoing both require a cross-drive copy.
    def test_cross_drive_round_trip(self):
        output = self.root / "sorted"
        output.mkdir()
        self.config["destination_root"] = str(output)
        source = self.write("lecture.pdf")
        self.real_move = organizer._move_no_replace
        with patch.object(organizer, "_move_no_replace", side_effect=self.cross_drive_move):
            result = organizer.execute_moves(self.scan(), self.history)
            self.assertEqual(result["errors"], [])
            self.assertIn("target_signature", result["moved"][0])
            result = organizer.undo_last_batch(self.history)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["restored"], [str(source)])
        self.assertEqual(source.read_text(), "original")
        self.assertFalse(list(output.rglob("*")))

    # If removing a copied source fails, undo should keep the original and remove only its verified copy.
    def test_cross_drive_interrupted_source_removal(self):
        output = self.root / "sorted"
        output.mkdir()
        self.config["destination_root"] = str(output)
        source = self.write("lecture.pdf")
        self.real_move = organizer._move_no_replace
        real_unlink = Path.unlink

        # Simulate a source locked after copying, while allowing temporary-file cleanup.
        def locked_unlink(path, *args, **kwargs):
            if path == source:
                raise PermissionError("source locked")
            return real_unlink(path, *args, **kwargs)

        with patch.object(organizer, "_move_no_replace", side_effect=self.cross_drive_move), \
                patch.object(Path, "unlink", locked_unlink):
            result = organizer.execute_moves(self.scan(), self.history)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(source.exists())
        self.assertTrue((output / "Documents/lecture.pdf").exists())
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(source)])
        self.assertEqual(source.read_text(), "original")
        self.assertFalse(list(output.rglob("*")))

    # Failed copies must leave the original untouched and remove incomplete staging files.
    def test_cross_drive_copy_failure(self):
        output = self.root / "sorted"
        output.mkdir()
        self.config["destination_root"] = str(output)
        source = self.write("lecture.pdf")
        self.real_move = organizer._move_no_replace
        with patch.object(organizer, "_move_no_replace", side_effect=self.cross_drive_move), \
                patch.object(organizer.shutil, "copyfileobj", side_effect=OSError("disk full")):
            result = organizer.execute_moves(self.scan(), self.history)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(source.read_text(), "original")
        self.assertEqual(list((output / "Documents").iterdir()), [])

    # Retry an interrupted cross-drive undo after its restored copy has already been published.
    def test_cross_drive_interrupted_undo(self):
        output = self.root / "sorted"
        output.mkdir()
        self.config["destination_root"] = str(output)
        source = self.write("lecture.pdf")
        target = output / "Documents/lecture.pdf"
        self.real_move = organizer._move_no_replace
        real_unlink = Path.unlink

        # Leave both verified copies behind at the final removal step of undo.
        def locked_unlink(path, *args, **kwargs):
            if path == target:
                raise PermissionError("organized copy locked")
            return real_unlink(path, *args, **kwargs)

        with patch.object(organizer, "_move_no_replace", side_effect=self.cross_drive_move):
            organizer.execute_moves(self.scan(), self.history)
            with patch.object(Path, "unlink", locked_unlink):
                result = organizer.undo_last_batch(self.history)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(source.exists())
        self.assertTrue(target.exists())
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(source)])
        self.assertEqual(source.read_text(), "original")
        self.assertFalse(target.exists())

    # Old journal entries have no destination field and must remain undoable after this upgrade.
    def test_legacy_history_remains_undoable(self):
        source = self.write("lecture.pdf")
        organizer.execute_moves(self.scan(), self.history)
        data = json.loads(self.history.read_text(encoding="utf-8"))
        del data["batches"][0]["moves"][0]["destination_root"]
        organizer.atomic_write_json(self.history, data)
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(source)])

    # Prove preview is read-only and skips temporary, hidden, and nested files.
    def test_scan_is_read_only_and_top_level(self):
        for name in ["Lecture.PDF", "script.py", "mystery.xyz", ".hidden.pdf", "file.part", "x.crdownload", "x.tmp", "desktop.ini", "link.lnk"]:
            self.write(name)
        self.write("nested.pdf", folder=self.folder / "existing")
        before = sorted(str(p.relative_to(self.folder)) for p in self.folder.rglob("*"))
        plans = self.scan()
        self.assertEqual({p["filename"] for p in plans}, {"Lecture.PDF", "script.py", "mystery.xyz"})
        self.assertEqual(before, sorted(str(p.relative_to(self.folder)) for p in self.folder.rglob("*")))
        self.assertFalse(self.history.exists())

    # Preserve a pre-existing duplicate and restore the new file to its exact original path.
    def test_duplicate_round_trip_and_preserve_existing_directory(self):
        source = self.write("report.pdf", "new")
        existing = self.write("report.pdf", "old", self.folder / "Documents")
        plan = self.scan()
        self.assertEqual(Path(plan[0]["target_path"]).name, "report (1).pdf")
        result = organizer.execute_moves(plan, self.history)
        self.assertEqual(len(result["moved"]), 1)
        self.assertEqual(existing.read_text(), "old")
        restored = organizer.undo_last_batch(self.history)
        self.assertEqual(restored["restored"], [str(source)])
        self.assertEqual(source.read_text(), "new")
        self.assertTrue(existing.parent.exists())

    # Recheck destination names because another process can create a duplicate after preview.
    def test_collision_after_preview(self):
        self.write("report.pdf")
        plan = self.scan()
        self.write("report.pdf", "external", self.folder / "Documents")
        result = organizer.execute_moves(plan, self.history)
        self.assertEqual(Path(result["moved"][0]["target_path"]).name, "report (1).pdf")
        self.assertEqual((self.folder / "Documents/report.pdf").read_text(), "external")

    # Reject changed source files so the approved preview cannot silently refer to different data.
    def test_stale_preview(self):
        source = self.write("report.pdf")
        plan = self.scan()
        source.write_text("changed content", encoding="utf-8")
        result = organizer.execute_moves(plan, self.history)
        self.assertFalse(result["moved"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(source.exists())

    # Keep undo conflicts retryable and never replace a file occupying the original path.
    def test_undo_conflict_then_retry(self):
        source = self.write("report.pdf")
        organizer.execute_moves(self.scan(), self.history)
        source.write_text("new occupant", encoding="utf-8")
        result = organizer.undo_last_batch(self.history)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(source.read_text(), "new occupant")
        source.unlink()
        self.assertEqual(len(organizer.undo_last_batch(self.history)["restored"]), 1)
        self.assertEqual(source.read_text(), "original")
        self.assertFalse((self.folder / "Documents").exists())

    # Leave edited organized files in place rather than treating them as the original journal entry.
    def test_undo_edited_target(self):
        self.write("report.pdf")
        result = organizer.execute_moves(self.scan(), self.history)
        target = Path(result["moved"][0]["target_path"])
        target.write_text("edited after organizing", encoding="utf-8")
        undo = organizer.undo_last_batch(self.history)
        self.assertFalse(undo["restored"])
        self.assertEqual(len(undo["errors"]), 1)
        self.assertTrue(target.exists())

    # Fail before moving anything if the history file is corrupted.
    def test_corrupt_history_stops_moves(self):
        source = self.write("report.pdf")
        self.history.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            organizer.execute_moves(self.scan(), self.history)
        self.assertTrue(source.exists())
        self.assertEqual(self.history.read_text(), "{bad")

    # Verify that denied journal writes stop the batch before a file is touched.
    def test_unwritable_journal_stops_moves(self):
        source = self.write("report.pdf")
        with patch.object(organizer, "atomic_write_json", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                organizer.execute_moves(self.scan(), self.history)
        self.assertTrue(source.exists())

    # Recover a move if the process stops after rename but before saving its success state.
    def test_recover_after_post_move_journal_failure(self):
        source = self.write("report.pdf")
        real_write = organizer.atomic_write_json
        count = 0

        # Inject one persistence failure at the exact point where the intent is already durable.
        def fail_after_move(path, data):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("simulated journal failure")
            real_write(path, data)

        with patch.object(organizer, "atomic_write_json", side_effect=fail_after_move):
            with self.assertRaises(OSError):
                organizer.execute_moves(self.scan(), self.history)
        self.assertFalse(source.exists())
        self.assertEqual(len(organizer.undo_last_batch(self.history)["restored"]), 1)
        self.assertEqual(source.read_text(), "original")

    # Keep successful entries undoable when a different file in the same batch cannot move.
    def test_partial_batch(self):
        self.write("a.pdf")
        self.write("b.pdf")
        real_move = organizer._move_no_replace

        # Simulate a file locked by a downloader while allowing the other move to finish.
        def locked_file(source, target):
            if source.name == "b.pdf":
                raise PermissionError("download is still open")
            real_move(source, target)

        with patch.object(organizer, "_move_no_replace", side_effect=locked_file):
            result = organizer.execute_moves(self.scan(), self.history)
        self.assertEqual(len(result["moved"]), 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(organizer.undo_last_batch(self.history)["restored"]), 1)
        self.assertTrue((self.folder / "b.pdf").exists())

    # Undo batches in reverse order and preserve unrelated files in category directories.
    def test_latest_batch_first_and_empty_directory_cleanup(self):
        first = self.write("a.pdf")
        organizer.execute_moves(self.scan(), self.history)
        second = self.write("b.png")
        organizer.execute_moves(self.scan(), self.history)
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(second)])
        self.assertFalse(first.exists())
        self.assertFalse((self.folder / "Images").exists())
        self.write("unrelated.txt", folder=self.folder / "Documents")
        organizer.undo_last_batch(self.history)
        self.assertTrue(first.exists())
        self.assertTrue((self.folder / "Documents/unrelated.txt").exists())

    # Reject category traversal, Windows device names, and ambiguous extension mappings.
    def test_invalid_rules(self):
        for category in ["../escape", "CON", "Images.", "a/b", "AUX.txt"]:
            with self.subTest(category=category), self.assertRaises(ValueError):
                organizer.validate_config({"categories": {category: [], "Others": []}})
        with self.assertRaises(ValueError):
            organizer.validate_config({"categories": {"A": [".pdf"], "B": [".PDF"], "Others": []}})

    # Refuse crafted plans that escape the source folder even if execute_moves is called directly.
    def test_tampered_plan(self):
        source = self.write("report.pdf")
        plan = self.scan()
        plan[0]["target_path"] = str(self.root / "escape.pdf")
        result = organizer.execute_moves(plan, self.history)
        self.assertFalse(result["moved"])
        self.assertTrue(source.exists())

    # Deduplicate selected folders so a source is never planned twice.
    def test_duplicate_folder_selection(self):
        self.write("report.pdf")
        self.assertEqual(len(organizer.scan_folders([self.folder, self.folder], self.config)), 1)

    # Check Windows hidden attributes separately from dotfile naming.
    @unittest.skipUnless(os.name == "nt", "Windows file attributes")
    def test_windows_hidden_file(self):
        import ctypes
        source = self.write("hidden.pdf")
        self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(str(source), 2))
        try:
            self.assertEqual(self.scan(), [])
        finally:
            ctypes.windll.kernel32.SetFileAttributesW(str(source), 128)

    # Never overwrite a destination even when it appears after duplicate-name resolution.
    def test_no_replace_primitive(self):
        source = self.write("a.pdf", "source")
        target = self.write("b.pdf", "destination")
        with self.assertRaises(FileExistsError):
            organizer._move_no_replace(source, target)
        self.assertEqual(source.read_text(), "source")
        self.assertEqual(target.read_text(), "destination")


if __name__ == "__main__":
    unittest.main()
