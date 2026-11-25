"""Folder grouping regressions use disposable roots and never personal folders."""

import errno
import ctypes
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import organizer


class FolderTests(unittest.TestCase):
    # Keep sources, custom destinations, and journal data isolated for each test.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.history = self.root / "history.json"
        self.config = {"categories": {"Documents": [".pdf"], "Others": []},
                       "organize_folders": True, "folders_category_name": "Folders"}

    # Include nested data, an empty folder, and hidden contents to verify whole-folder preservation.
    def make_folder(self, path):
        path.mkdir(parents=True)
        (path / "nested").mkdir()
        (path / "empty").mkdir()
        (path / "nested/notes.txt").write_text("lecture notes", encoding="utf-8")
        (path / ".settings").write_text("hidden content", encoding="utf-8")
        return path

    # Use the public scan function so fixtures exercise real classification and exclusion rules.
    def scan(self):
        return organizer.scan_folders([self.inbox], self.config)

    # Grouping remains opt-in, including configurations saved before this feature existed.
    def test_grouping_disabled_by_default(self):
        self.make_folder(self.inbox / "Lectures")
        self.config.pop("organize_folders")
        self.assertEqual(self.scan(), [])
        self.config["organize_folders"] = False
        self.assertEqual(self.scan(), [])
        self.assertFalse((self.inbox / "Folders").exists())

    # Preview is read-only, and undo returns a populated folder and all its contents exactly.
    def test_group_and_undo(self):
        source = self.make_folder(self.inbox / "Lectures")
        plans = self.scan()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["category"], "Folders")
        self.assertEqual(plans[0]["item_type"], "directory")
        target = self.inbox / "Folders/Lectures"
        self.assertEqual(plans[0]["target_path"], str(target))
        self.assertTrue(source.exists())
        self.assertFalse(target.parent.exists())
        moved = organizer.execute_moves(plans, self.history)
        self.assertEqual(moved["errors"], [])
        self.assertFalse(source.exists())
        self.assertEqual((target / "nested/notes.txt").read_text(), "lecture notes")
        self.assertEqual(self.scan(), [])
        restored = organizer.undo_last_batch(self.history)
        self.assertEqual(restored["errors"], [])
        self.assertEqual(restored["restored"], [str(source)])
        self.assertEqual((source / ".settings").read_text(), "hidden content")
        self.assertTrue((source / "empty").is_dir())
        self.assertFalse(target.parent.exists())

    # Protect standard names even when absent from rules, plus custom categories, regardless of case.
    def test_category_folders_never_grouped(self):
        self.config["categories"]["Research"] = [".tex"]
        names = ["documents", "CODE", "Images", "Archives", "Installers", "Media", "Others", "Folders", "Research"]
        for name in names:
            self.make_folder(self.inbox / name)
        loose = self.make_folder(self.inbox / "Semester 1")
        plans = self.scan()
        self.assertEqual([plan["source_path"] for plan in plans], [str(loose)])
        organizer.execute_moves(plans, self.history)
        for name in names:
            self.assertTrue((self.inbox / name / "nested/notes.txt").is_file())
        self.assertFalse((self.inbox / "Folders/Folders").exists())

    # Skip hidden/excluded folders and ancestors containing excluded data or the output root.
    def test_hidden_excluded_and_destination_folders_skipped(self):
        self.make_folder(self.inbox / ".hidden")
        excluded = self.make_folder(self.inbox / "Private")
        protected = self.make_folder(self.inbox / "Protected")
        output = self.inbox / "Output container" / "Sorted"
        output.mkdir(parents=True)
        loose = self.make_folder(self.inbox / "Loose")
        self.config["destination_root"] = str(output)
        self.config["excluded_paths"] = [str(excluded), str(protected / "nested/notes.txt")]
        self.assertEqual([p["source_path"] for p in self.scan()], [str(loose)])
        self.config["destination_root"] = str(output.parent)
        self.assertEqual([p["source_path"] for p in self.scan()], [str(loose)])

    # Excluding an ancestor protects its contents even when a nested source is explicitly selected.
    def test_excluded_ancestor_protects_selected_source(self):
        self.make_folder(self.inbox / "Lectures")
        (self.inbox / "notes.pdf").write_text("notes", encoding="utf-8")
        self.config["excluded_paths"] = [str(self.root)]
        self.assertEqual(self.scan(), [])

    # Windows hidden and system attributes must protect folders even without a leading dot.
    @unittest.skipUnless(os.name == "nt", "Windows file attributes")
    def test_windows_hidden_and_system_folders_skipped(self):
        for name, attribute in [("Hidden", 0x2), ("System", 0x4)]:
            source = self.make_folder(self.inbox / name)
            original = source.stat().st_file_attributes
            set_attributes = ctypes.windll.kernel32.SetFileAttributesW
            self.assertTrue(set_attributes(str(source), original | attribute))
            try:
                self.assertNotIn(str(source), [p["source_path"] for p in self.scan()])
            finally:
                self.assertTrue(set_attributes(str(source), original))
    # Number whole folder names rather than treating dotted folder names as file extensions.
    def test_duplicate_directory_names(self):
        for name in ["Lectures", "Project.v1"]:
            with self.subTest(name=name):
                source = self.make_folder(self.inbox / name)
                existing = self.make_folder(self.inbox / "Folders" / name)
                plans = self.scan()
                plan = next(item for item in plans if item["source_path"] == str(source))
                self.assertEqual(Path(plan["target_path"]).name, name + " (1)")
                result = organizer.execute_moves([plan], self.history)
                self.assertEqual(result["errors"], [])
                self.assertEqual(len(organizer.undo_last_batch(self.history)["restored"]), 1)
                self.assertTrue(existing.exists())
                self.assertTrue(source.exists())

    # Recheck folder collisions at execution and keep an occupied undo path untouched until retry.
    def test_late_collision_and_undo_conflict(self):
        source = self.make_folder(self.inbox / "Lectures")
        plans = self.scan()
        self.make_folder(self.inbox / "Folders/Lectures")
        moved = organizer.execute_moves(plans, self.history)
        target = Path(moved["moved"][0]["target_path"])
        self.assertEqual(target.name, "Lectures (1)")
        source.mkdir()
        self.assertEqual(len(organizer.undo_last_batch(self.history)["errors"]), 1)
        self.assertTrue(target.exists())
        source.rmdir()
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(source)])

    # Honor a customized grouping name and persist both settings without losing them in validation.
    def test_custom_grouping_name_and_destination(self):
        output = self.root / "sorted"
        output.mkdir()
        self.config.update(destination_root=str(output), folders_category_name="Collections")
        source = self.make_folder(self.inbox / "Lectures")
        self.make_folder(self.inbox / "Collections")
        config_file = self.root / "config.json"
        organizer.atomic_write_json(config_file, organizer.validate_config(self.config))
        self.config = organizer.load_config(config_file)
        self.assertTrue(self.config["organize_folders"])
        self.assertEqual(self.config["folders_category_name"], "Collections")
        plans = self.scan()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["target_path"], str(output / "Collections/Lectures"))
        self.assertEqual(organizer.execute_moves(plans, self.history)["errors"], [])
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(source)])
        self.assertTrue(output.exists())

    # Keep explicitly selected nested roots in place so their own plans remain valid.
    def test_overlapping_sources_not_grouped(self):
        nested = self.make_folder(self.inbox / "Selected")
        (nested / "lecture.pdf").write_text("pdf", encoding="utf-8")
        plans = organizer.scan_folders([self.inbox, nested], self.config)
        self.assertNotIn(str(nested), [p["source_path"] for p in plans])
        self.assertIn(str(nested / "lecture.pdf"), [p["source_path"] for p in plans])

    # The history file must never be moved indirectly as part of a grouped directory.
    def test_folder_containing_history_is_protected(self):
        source = self.make_folder(self.inbox / "Journal holder")
        plans = self.scan()
        result = organizer.execute_moves(plans, source / "history.json")
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(source.exists())

    # Reject malformed opt-in flags and unsafe folder names instead of guessing the user's intent.
    def test_invalid_folder_settings(self):
        for value in [1, "true", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                organizer.validate_config(dict(self.config, organize_folders=value))
        for name in ["../Elsewhere", "", ".hidden", "CON", "Folders."]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                organizer.validate_config(dict(self.config, folders_category_name=name))

    # A journal intent still identifies a whole folder if the final success write is interrupted.
    def test_folder_recovery_after_journal_failure(self):
        source = self.make_folder(self.inbox / "Lectures")
        real_write, count = organizer.atomic_write_json, 0

        # Fail only after the rename has happened, leaving its earlier intent recoverable.
        def fail_after_rename(path, data):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("interrupted success write")
            real_write(path, data)

        with patch.object(organizer, "atomic_write_json", side_effect=fail_after_rename):
            with self.assertRaises(OSError):
                organizer.execute_moves(self.scan(), self.history)
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(source)])

    # A folder swapped after preview is a different item, even if it has the same name.
    def test_replaced_folder_after_preview(self):
        source = self.make_folder(self.inbox / "Lectures")
        plans = self.scan()
        source.rename(self.root / "Original folder")
        source.mkdir()
        result = organizer.execute_moves(plans, self.history)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(source.exists())
        self.assertTrue((self.root / "Original folder/nested/notes.txt").exists())

    # Whole-folder undo keeps edits made inside the same folder rather than discarding new work.
    def test_undo_preserves_new_folder_contents(self):
        source = self.make_folder(self.inbox / "Lectures")
        result = organizer.execute_moves(self.scan(), self.history)
        target = Path(result["moved"][0]["target_path"])
        (target / "new notes.txt").write_text("new work", encoding="utf-8")
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(source)])
        self.assertEqual((source / "new notes.txt").read_text(), "new work")

    # Simulate separate drives while preserving the atomic same-directory publish of staging folders.
    def cross_drive_move(self, source, target):
        if source.parent != target.parent:
            raise OSError(errno.EXDEV, "simulated cross-drive move")
        self.real_move(source, target)

    # Whole directory copies preserve nested and hidden contents and remain undoable across drives.
    def test_cross_drive_folder_round_trip(self):
        output = self.root / "sorted"
        output.mkdir()
        self.config["destination_root"] = str(output)
        source = self.make_folder(self.inbox / "Lectures")
        self.real_move = organizer._move_no_replace
        with patch.object(organizer, "_move_no_replace", side_effect=self.cross_drive_move):
            result = organizer.execute_moves(self.scan(), self.history)
            self.assertEqual(result["errors"], [])
            self.assertFalse(source.exists())
            result = organizer.undo_last_batch(self.history)
            self.assertEqual(result["errors"], [])
        self.assertEqual((source / "nested/notes.txt").read_text(), "lecture notes")
        self.assertEqual((source / ".settings").read_text(), "hidden content")
        self.assertTrue((source / "empty").is_dir())
        self.assertFalse(list(output.iterdir()))

    # Source cleanup failures must preserve the complete published copy as well as the original data.
    def test_cross_drive_folder_cleanup_failure(self):
        source = self.make_folder(self.inbox / "Lectures")
        self.real_move = organizer._move_no_replace
        with patch.object(organizer, "_move_no_replace", side_effect=self.cross_drive_move), \
                patch.object(organizer, "_remove_tree_verified", side_effect=PermissionError("locked")):
            result = organizer.execute_moves(self.scan(), self.history)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(source.exists())
        self.assertTrue((self.inbox / "Folders/Lectures/nested/notes.txt").exists())
        self.assertEqual(organizer.undo_last_batch(self.history)["restored"], [str(source)])

    # A partially removed original is a conflict, never a reason to discard its complete backup.
    def test_partial_source_cleanup_keeps_backup(self):
        source = self.make_folder(self.inbox / "Lectures")
        self.real_move = organizer._move_no_replace

        # Model a cleanup interruption after one original file has been removed.
        def partial_cleanup(path, snapshot):
            (path / ".settings").unlink()
            raise PermissionError("another item is locked")

        with patch.object(organizer, "_move_no_replace", side_effect=self.cross_drive_move), \
                patch.object(organizer, "_remove_tree_verified", side_effect=partial_cleanup):
            result = organizer.execute_moves(self.scan(), self.history)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(organizer.undo_last_batch(self.history)["errors"]), 1)
        self.assertEqual((self.inbox / "Folders/Lectures/.settings").read_text(), "hidden content")

    # Windows hidden attributes protect folders even when their names do not start with a dot.
    @unittest.skipUnless(os.name == "nt", "Windows file attributes")
    def test_windows_hidden_folder(self):
        import ctypes
        source = self.make_folder(self.inbox / "Hidden")
        self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(str(source), 2))
        try:
            self.assertEqual(self.scan(), [])
        finally:
            ctypes.windll.kernel32.SetFileAttributesW(str(source), 128)


if __name__ == "__main__":
    unittest.main()
