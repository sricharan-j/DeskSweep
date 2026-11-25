"""Exercise real widgets and asynchronous actions using an invisible test window."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import customtkinter as ctk
import main


class GuiTests(unittest.TestCase):
    # Pump Tk events with a deadline so worker completion can be tested without a manual mainloop.
    def pump_until(self, app, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        finished = []

        # Tests use bounded checks; production notifications require Tk's real main event loop.
        def check():
            if predicate():
                finished.append(True)
                app.quit()
            elif time.monotonic() >= deadline:
                app.quit()
            else:
                app.after(20, check)

        app.after(20, check)
        main.tk.Tk.mainloop(app)
        self.assertTrue(finished, "GUI operation did not finish before its deadline")

    # Verify preview, organize, undo, rules, watcher shutdown, and close without touching personal files.
    def test_window_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "demo"
            inbox.mkdir()
            source = inbox / "lecture.pdf"
            source.write_text("dummy lecture", encoding="utf-8")
            config_file = root / "config.json"
            config_file.write_text(json.dumps({"categories": {"Documents": [".pdf"], "Others": []},
                                                "ignored_extensions": []}), encoding="utf-8")
            with patch.object(main, "CONFIG_FILE", config_file), patch.object(main, "HISTORY_FILE", root / "history.json"), \
                    patch.object(main.messagebox, "showerror") as errors, patch.object(main.messagebox, "showwarning") as warnings:
                ctk.set_appearance_mode("dark")
                app = main.DeskSweepApp()
                app.withdraw()
                try:
                    self.assertFalse(any(var.get() for var in app.folder_vars.values()))
                    self.assertFalse(app.watch_var.get())
                    self.assertFalse(app.group_folders_var.get())
                    self.assertEqual(app.group_folders_switch.cget("text"), "Group existing folders into /Folders")
                    self.assertEqual(app.destination_var.get(), "Same as source folder (Default)")
                    app._add_folder(inbox, selected=True)
                    app._scan()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertEqual(len(app.table.get_children()), 1)
                    self.assertTrue(source.exists())
                    self.assertEqual(app.organize_button.cget("state"), "normal")
                    output = root / "sorted"
                    output.mkdir()
                    with patch.object(main.filedialog, "askdirectory", return_value=str(output)):
                        app._choose_destination()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertEqual(app.destination_var.get(), str(output))
                    self.assertEqual(json.loads(config_file.read_text())["destination_root"], str(output))
                    self.assertEqual(app.planned_moves[0]["target_path"], str(output / "Documents/lecture.pdf"))
                    self.assertEqual(app.table.item(app.table.get_children()[0])["values"][2], str(output / "Documents/lecture.pdf"))
                    self.assertTrue(source.exists())
                    app._organize()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertTrue((output / "Documents/lecture.pdf").exists())
                    app._undo()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertTrue(source.exists())
                    app.destination_reset.invoke()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertEqual(app.destination_var.get(), "Same as source folder (Default)")
                    self.assertEqual(json.loads(config_file.read_text())["destination_root"], "")
                    self.assertEqual(app.planned_moves[0]["target_path"], str(inbox / "Documents/lecture.pdf"))
                    app._organize()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertTrue((inbox / "Documents/lecture.pdf").exists())
                    app._undo()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertTrue(source.exists())
                    self.assertFalse((inbox / "Documents").exists())
                    loose = inbox / "Course materials"
                    loose.mkdir()
                    (loose / "notes.txt").write_text("folder contents", encoding="utf-8")
                    app.group_folders_switch.toggle()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertTrue(json.loads(config_file.read_text())["organize_folders"])
                    self.assertEqual(json.loads(config_file.read_text())["folders_category_name"], "Folders")
                    self.assertEqual(len(app.planned_moves), 2)
                    self.assertIn(str(inbox / "Folders/Course materials"),
                                  [move["target_path"] for move in app.planned_moves])
                    self.assertTrue(loose.exists())
                    app.group_folders_switch.toggle()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertFalse(json.loads(config_file.read_text())["organize_folders"])
                    self.assertEqual(len(app.planned_moves), 1)
                    app.group_folders_switch.toggle()
                    self.pump_until(app, lambda: not app.busy)
                    app._organize()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertTrue((inbox / "Folders/Course materials/notes.txt").exists())
                    app._undo()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertEqual((loose / "notes.txt").read_text(), "folder contents")
                    self.assertFalse((inbox / "Folders").exists())
                    # A failed save restores the switch to the last persisted setting.
                    with patch.object(main, "atomic_write_json", side_effect=PermissionError("settings locked")):
                        app.group_folders_switch.toggle()
                        self.pump_until(app, lambda: not app.busy)
                    self.assertTrue(app.group_folders_var.get())
                    self.assertTrue(json.loads(config_file.read_text())["organize_folders"])
                    errors.assert_called_once()
                    errors.reset_mock()
                    app._edit_rules()
                    app.rules_window.withdraw()
                    self.assertTrue(app.rules_window.winfo_exists())
                    app.rules_window.destroy()
                    app.rules_window = None
                    app.watch_var.set(True)
                    app._toggle_watch()
                    self.pump_until(app, lambda: not app.busy)
                    watcher = app.watcher
                    self.assertIsNotNone(watcher)
                    self.assertEqual(watcher.settle_seconds, 3.0)
                    self.assertEqual(app.settle_apply.cget("state"), "normal")
                    app.settle_var.set("0.75")
                    app.settle_apply.invoke()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertIs(app.watcher, watcher)
                    self.assertEqual(watcher.settle_seconds, 0.75)
                    self.assertEqual(json.loads(config_file.read_text())["settle_seconds"], 0.75)
                    app.settle_var.set("nan")
                    app.settle_apply.invoke()
                    self.assertFalse(app.busy)
                    self.assertEqual(watcher.settle_seconds, 0.75)
                    self.assertEqual(json.loads(config_file.read_text())["settle_seconds"], 0.75)
                    # Failed persistence must keep the running watcher's previous safety pause.
                    with patch.object(main, "atomic_write_json", side_effect=PermissionError("settings locked")):
                        app.settle_var.set("5")
                        app.settle_apply.invoke()
                        self.pump_until(app, lambda: not app.busy)
                    self.assertEqual(watcher.settle_seconds, 0.75)
                    errors.assert_called_once()
                    errors.reset_mock()
                    self.assertEqual(app.destination_browse.cget("state"), "disabled")
                    self.assertEqual(app.group_folders_switch.cget("state"), "disabled")
                    app._undo()
                    self.pump_until(app, lambda: not app.busy)
                    self.assertIsNone(app.watcher)
                    self.assertFalse(app.watch_var.get())
                    self.assertFalse(watcher._worker.is_alive())
                    self.assertFalse(watcher._observer.is_alive())
                    errors.assert_not_called()
                    warnings.assert_not_called()
                finally:
                    app._quit()
                    main.tk.Tk.mainloop(app)
                    self.assertTrue(app.executor._shutdown)


if __name__ == "__main__":
    unittest.main()
