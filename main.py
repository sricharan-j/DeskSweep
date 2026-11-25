"""DeskSweep's dark desktop interface. Widget updates stay on the main thread."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from organizer import atomic_write_json, destination_directory, execute_moves, load_config, scan_folders, undo_last_batch, validate_config
from watcher import FolderWatcher
from tray import TrayController
from ui_events import EventDrivenWindow, EventDrivenToplevel, EventDrivenTextbox, UiEvents

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
HISTORY_FILE = APP_DIR / "history.json"
VERSION = "1.1"
THEME = {
    "background": "#000000", "panel": "#080A0D", "border": "#1A212B",
    "text": "#F0F5FA", "muted": "#8291A3", "accent": "#38BDF8",
    "accent_hover": "#7DD3FC", "accent_text": "#04121C", "indigo": "#0284C7",
    "button": "#11161D", "button_hover": "#1B232E", "heading": "#0D1117",
    "selection": "#12344D", "selected_text": "#FFFFFF", "status": "#040507",
    "error": "#FF8A80", "transparent": "transparent",
}

DEFAULT_DESTINATION = "Same as source folder (Default)"


# Apply the shared palette to widget defaults so dialogs and controls also use the indigo theme.
def apply_theme():
    colors = {
        "text_color": "text", "text_color_disabled": "muted", "border_color": "border",
        "hover_color": "accent_hover", "progress_color": "indigo", "checkmark_color": "accent_text",
        "button_color": "muted", "button_hover_color": "text", "placeholder_text_color": "muted",
        "scrollbar_button_color": "border", "scrollbar_button_hover_color": "muted",
        "top_fg_color": "panel", "label_fg_color": "panel",
    }
    for name, widget in ctk.ThemeManager.theme.items():
        if not name.startswith("CTk"):
            continue
        for key, color in colors.items():
            if key in widget:
                widget[key] = THEME[color]
        if "fg_color" in widget:
            color = {"CTk": "background", "CTkToplevel": "background", "CTkButton": "accent",
                     "CTkCheckBox": "indigo", "CTkSwitch": "border", "CTkLabel": "transparent",
                     "CTkScrollbar": "transparent"}.get(name, "panel")
            widget["fg_color"] = THEME[color]
    ctk.ThemeManager.theme["CTkButton"]["text_color"] = THEME["accent_text"]


class DeskSweepApp(EventDrivenWindow, ctk.CTk):
    # Initialize UI state before building widgets so background results always have a safe destination.
    def __init__(self):
        ctk.set_appearance_mode("dark")
        apply_theme()
        super().__init__()
        self.title(f"DeskSweep v{VERSION}")
        self.geometry("1120x760")
        self.minsize(900, 620)
        self.configure(fg_color=THEME["background"])
        self.config_data = None
        self.planned_moves = []
        self.watcher = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="DeskSweep-actions")
        self.events = UiEvents(self, self._drain_events)
        self.tray = None
        self._tray_ready = False
        self._tray_error = None
        self._hide_when_ready = False
        self._hidden_to_tray = False
        self.busy = False
        self.closing = False
        self.folder_vars = {}
        self.folder_widgets = []
        self.rules_window = None
        self._render_token = 0
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Configure>", self._update_dpi, add="+")
        self.bind("<Map>", self._update_dpi, add="+")
        try:
            self.config_data = load_config(CONFIG_FILE)
            self.destination_var.set(self.config_data["destination_root"] or DEFAULT_DESTINATION)
            self.settle_var.set(f"{self.config_data['settle_seconds']:g}")
        except (OSError, ValueError) as exc:
            self._status(f"Rules need attention: {exc}. Open Edit Rules to fix them.")
        self._refresh_controls()

    # Assemble a quiet sidebar, readable preview, and persistent action/status area.
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        sidebar = ctk.CTkFrame(self, width=250, corner_radius=1, fg_color=THEME["panel"])
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(sidebar, text="DeskSweep", font=("Consolas", 40, "bold"), text_color=THEME["text"]).grid(row=0, column=0, sticky="w", padx=24, pady=(30, 0))
        ctk.CTkLabel(sidebar, text="SELECTED FOLDERS", font=("Poppins", 10, "bold"), text_color=THEME["muted"]).grid(row=2, column=0, sticky="w", padx=20, pady=(16, 0))
        self.folder_list = ctk.CTkScrollableFrame(sidebar, fg_color=THEME["transparent"], height=220)
        self.folder_list.grid(row=3, column=0, sticky="nsew", padx=14, pady=12)
        self._add_folder(Path.home() / "Downloads", "Downloads")
        self._add_folder(Path.home() / "Desktop", "Desktop")
        self.add_button = ctk.CTkButton(sidebar, text="+  Add a folder", command=self._choose_folder,
                                         fg_color=THEME["button"], text_color=THEME["text"], hover_color=THEME["button_hover"], height=36)
        self.add_button.grid(row=4, column=0, sticky="ew", padx=24)
        ctk.CTkFrame(sidebar, height=1, fg_color=THEME["border"]).grid(row=5, column=0, sticky="ew", padx=24, pady=20)
        self.watch_var = tk.BooleanVar(value=False)
        self.watch_switch = ctk.CTkSwitch(sidebar, text="Auto-Watch Mode", variable=self.watch_var,
                                         command=self._toggle_watch, progress_color=THEME["indigo"])
        self.watch_switch.grid(row=6, column=0, sticky="w", padx=24)
        ctk.CTkLabel(sidebar, text="Automatically organizes new arrivals after a short safety pause. Stays active while DeskSweep is open or in the system tray.", wraplength=200,
                     justify="left", text_color=THEME["muted"], font=("Segoe UI", 12)).grid(row=7, column=0, sticky="w", padx=24, pady=12)
        pause_row = ctk.CTkFrame(sidebar, fg_color=THEME["transparent"])
        pause_row.grid(row=8, column=0, sticky="ew", padx=24)
        ctk.CTkLabel(pause_row, text="Safety pause (seconds)", text_color=THEME["muted"]).pack(anchor="w")
        self.settle_var = tk.StringVar(value="3")
        self.settle_entry = ctk.CTkEntry(pause_row, textvariable=self.settle_var, width=90)
        self.settle_entry.pack(side="left", pady=(4, 0))
        self.settle_apply = ctk.CTkButton(pause_row, text="Apply", width=80, command=self._set_settle_delay)
        self.settle_apply.pack(side="right", pady=(4, 0))
        self.rules_button = ctk.CTkButton(sidebar, text="Edit Rules", command=self._edit_rules,
                                          fg_color=THEME["transparent"], text_color=THEME["text"], border_width=1, border_color=THEME["border"])
        self.rules_button.grid(row=9, column=0, sticky="ew", padx=24, pady=20)
        content = ctk.CTkFrame(self, fg_color=THEME["transparent"])
        content.grid(row=0, column=1, sticky="nsew", padx=30, pady=(28, 20))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(4, weight=1)
        destination_row = ctk.CTkFrame(content, fg_color=THEME["panel"])
        destination_row.grid(row=2, column=0, sticky="ew", pady=(0, 18))

        destination_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(destination_row, text="Output Destination", text_color=THEME["muted"]).grid(row=0, column=0, padx=(12, 8), pady=12)
        self.destination_var = tk.StringVar(value=DEFAULT_DESTINATION)
        self.destination_display = ctk.CTkEntry(destination_row, textvariable=self.destination_var,
                                                state="readonly", fg_color=THEME["panel"], border_width=0)
        self.destination_display.grid(row=0, column=1, sticky="ew", pady=12)
        self.destination_browse = ctk.CTkButton(destination_row, text="Browse...", width=80,
                                                command=self._choose_destination, fg_color=THEME["button"], text_color=THEME["text"])
        self.destination_browse.grid(row=0, column=2, padx=8, pady=12)
        self.destination_reset = ctk.CTkButton(destination_row, text="Reset", width=60,
                                               command=lambda: self._set_destination(""), fg_color=THEME["button"], text_color=THEME["text"])
        self.destination_reset.grid(row=0, column=3, padx=(0, 12), pady=12)
        self.group_folders_var = tk.BooleanVar(value=False)
        self.group_folders_switch = ctk.CTkSwitch(
            destination_row, text="Group existing folders into /Folders", variable=self.group_folders_var,
            command=self._toggle_folder_grouping, progress_color=THEME["indigo"])
        self.group_folders_switch.grid(row=1, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 14))
        heading = ctk.CTkFrame(content, fg_color=THEME["transparent"])
        heading.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(heading, text="Move preview", font=("Segoe UI", 17, "bold")).pack(side="left")
        self.count_label = ctk.CTkLabel(heading, text="0 items", text_color=THEME["accent"])
        self.count_label.pack(side="right")
        table_frame = ctk.CTkFrame(content, fg_color=THEME["panel"], border_width=1, border_color=THEME["border"])
        table_frame.grid(row=4, column=0, sticky="nsew")
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)
        style = ttk.Style(self)
        style.theme_use("clam")
        scale = table_frame._get_widget_scaling()
        style.configure("Sweep.Treeview", background=THEME["panel"], fieldbackground=THEME["panel"], foreground=THEME["text"],
                        borderwidth=0, bordercolor=THEME["border"], lightcolor=THEME["panel"], darkcolor=THEME["panel"],
                        rowheight=round(36 * scale), font=("Segoe UI", -round(13 * scale)))
        style.configure("Sweep.Treeview.Heading", background=THEME["heading"], foreground=THEME["muted"],
                        borderwidth=0, padding=(round(10 * scale), round(10 * scale)),
                        font=("Segoe UI", -round(12 * scale), "bold"))
        style.map("Sweep.Treeview", background=[("selected", THEME["selection"])], foreground=[("selected", THEME["selected_text"])])
        self.table = ttk.Treeview(table_frame, columns=("file", "category", "destination"), show="headings", style="Sweep.Treeview")
        for name, title, width in [("file", "File / Folder", 250), ("category", "Category", 115), ("destination", "Destination", 390)]:
            self.table.heading(name, text=title, anchor="w")
            self.table.column(name, width=round(width * scale), minwidth=round(100 * scale),
                              anchor="w", stretch=name == "destination")
        self.table.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=(1, 0))
        vertical = ctk.CTkScrollbar(table_frame, command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ctk.CTkScrollbar(table_frame, orientation="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.empty_label = ctk.CTkLabel(table_frame, text="Your next clean slate starts here.\n\nSelect a folder and click Scan & Preview.\nNo files move during a scan.",
                                        text_color=THEME["muted"], font=("Segoe UI", 14), fg_color=THEME["panel"])
        self.empty_label.place(relx=0.5, rely=0.53, anchor="center")
        actions = ctk.CTkFrame(content, fg_color=THEME["transparent"])
        actions.grid(row=5, column=0, sticky="ew", pady=(20, 10))
        self.scan_button = ctk.CTkButton(actions, text="Scan & Preview", height=40, command=self._scan,
                                         fg_color=THEME["button"], text_color=THEME["text"], hover_color=THEME["button_hover"])
        self.scan_button.pack(side="left")
        self.organize_button = ctk.CTkButton(actions, text="Organize Now", height=40, command=self._organize,
                                             fg_color=THEME["button"], hover_color=THEME["accent_hover"], text_color=THEME["accent_text"])
        self.organize_button.pack(side="left", padx=10)
        self.undo_button = ctk.CTkButton(actions, text="Undo Last Run", height=40, command=self._undo,
                                         fg_color=THEME["transparent"], text_color=THEME["text"], border_width=1, border_color=THEME["border"])
        self.undo_button.pack(side="right")
        bottom = ctk.CTkFrame(self, fg_color=THEME["status"], corner_radius=0, height=40)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.status_label = ctk.CTkLabel(bottom, text="Ready. Start with a test folder; Auto-Watch is off.",
                                         text_color=THEME["muted"], anchor="w", wraplength=1000)
        self.status_label.pack(side="left", fill="x", expand=True, padx=22, pady=6)

    # Add an unchecked directory choice so opening the app cannot select personal files implicitly.
    def _add_folder(self, path, label=None, selected=False):
        path = Path(path).absolute()
        if path in self.folder_vars:
            self.folder_vars[path].set(selected)
            return
        variable = tk.BooleanVar(value=selected)
        self.folder_vars[path] = variable
        checkbox = ctk.CTkCheckBox(self.folder_list, text=label or path.name, variable=variable,
                                   command=self._folders_changed, checkbox_width=20, checkbox_height=20)
        checkbox.pack(anchor="w", padx=2, pady=(8, 2))
        ctk.CTkLabel(self.folder_list, text=str(path), text_color=THEME["muted"], font=("Segoe UI", 10),
                     wraplength=190, justify="left").pack(anchor="w", padx=2, pady=(0, 6))
        self.folder_widgets.append(checkbox)

    # Use the native folder picker so redirected Desktop or OneDrive paths can be selected explicitly.
    def _choose_folder(self):
        chosen = filedialog.askdirectory(parent=self, title="Choose a folder to organize", mustexist=True)
        if chosen:
            self._add_folder(chosen, selected=True)
            self._folders_changed()

    # Choose an existing output folder explicitly so category folders never appear in an accidental path.
    def _choose_destination(self):
        chosen = filedialog.askdirectory(parent=self, title="Choose destination folder", mustexist=True)
        if chosen:
            self._set_destination(chosen)

    # Save destination changes through the action worker to keep unavailable drives from freezing Tk.
    def _set_destination(self, path):
        if self.busy or self.closing or self.watcher is not None or self.config_data is None:
            return
        config = dict(self.config_data, destination_root=str(path))
        self._submit(lambda: self._write_config(config), self._settings_saved,
                     "Saving destination and refreshing the preview…")

    # Persist the grouping toggle and rebuild the preview without moving any items.
    def _toggle_folder_grouping(self):
        if self.busy or self.closing or self.watcher is not None or self.config_data is None:
            return
        config = dict(self.config_data, organize_folders=self.group_folders_var.get())
        self._submit(lambda: self._write_config(config), self._settings_saved,
                     "Saving folder grouping and refreshing the preview…")

    # Validate settings and save atomically before changing the displayed output mode.
    def _write_config(self, config):
        config = validate_config(config)
        destination_directory(config)
        atomic_write_json(CONFIG_FILE, config)
        return config

    # Save a positive delay on the action worker so users can adjust a running watch without restarting it.
    def _set_settle_delay(self):
        if self.busy or self.closing or self.config_data is None:
            return
        try:
            config = validate_config(dict(self.config_data, settle_seconds=float(self.settle_var.get())))
        except ValueError:
            self._status("Choose a safety pause greater than zero, such as 3 seconds.")
            return
        self._submit(lambda: self._write_config(config), self._settle_delay_saved, "Saving your safety pause…")

    # Apply saved delays under the watcher's lock and retain the current preview and watcher instance.
    def _settle_delay_saved(self, config):
        self.config_data = config
        seconds = config["settle_seconds"]
        self.settle_var.set(f"{seconds:g}")
        if self.watcher is not None:
            self.watcher.set_settle_seconds(seconds)
        self._status(f"Safety pause saved: {seconds:g} seconds. New arrivals will wait for this much quiet.")

    # Rescan selected sources after saving so the visible plan always uses the new settings.
    def _settings_saved(self, config):
        self.config_data = config
        self.destination_var.set(config["destination_root"] or DEFAULT_DESTINATION)
        self._clear_preview()
        if self._selected_folders() and not self.closing:
            self._scan()
        else:
            self._status("Settings saved. Select a source folder to preview moves.")
        self._refresh_controls()

    # Invalidate old previews when folder scope changes to avoid organizing an unexpected selection.
    def _folders_changed(self):
        self._clear_preview()
        self._status("Folder selection changed. Scan to preview the new selection.")
        self._refresh_controls()

    # Read selection on the UI thread before passing plain paths to a background task.
    def _selected_folders(self):
        return [path for path, variable in self.folder_vars.items() if variable.get()]

    # Protect application files even if the user chooses DeskSweep's own installation folder.
    def _effective_config(self):
        config = validate_config(self.config_data)
        config["excluded_paths"] += [str(APP_DIR / name) for name in
                                     ("main.py", "organizer.py", "watcher.py", "tray.py", "ui_events.py", "config.json", "requirements.txt",
                                      "README.md", "history.json", "history.json.lock")]
        return config

    # Keep control states consistent while tasks run or automatic organization is active.
    def _refresh_controls(self):
        idle = not self.busy and not self.closing
        manual = idle and self.watcher is None
        for widget in [self.add_button, self.rules_button, *self.folder_widgets]:
            widget.configure(state="normal" if manual else "disabled")
        self.scan_button.configure(state="normal" if manual and self.config_data else "disabled")
        self.organize_button.configure(state="normal" if manual and self.planned_moves else "disabled")
        self.undo_button.configure(state="normal" if idle else "disabled")
        self.watch_switch.configure(state="normal" if idle and self.config_data else "disabled")
        self.settle_entry.configure(state="normal" if idle and self.config_data else "disabled")
        self.settle_apply.configure(state="normal" if idle and self.config_data else "disabled")
        self.destination_browse.configure(state="normal" if manual and self.config_data else "disabled")
        self.destination_reset.configure(state="normal" if manual and self.config_data
                                          and self.config_data["destination_root"] else "disabled")
        self.group_folders_switch.configure(state="normal" if manual and self.config_data else "disabled")
        if self.config_data:
            self.group_folders_switch.configure(
                text=f"Group existing folders into /{self.config_data['folders_category_name']}")
            if not self.busy:
                self.group_folders_var.set(self.config_data["organize_folders"])
        if self.tray is not None:
            self.tray.update_state(self.watcher is not None, idle and bool(self.config_data))

    # Set status only from the main thread so Tk never races filesystem workers.
    def _status(self, text):
        self.status_label.configure(text=text)

    # Submit slow work once and route its completion back through the UI's event queue.
    def _submit(self, work, complete, status):
        if self.busy or self.closing:
            return
        self.busy = True
        self._status(status)
        self._refresh_controls()
        future = self.executor.submit(work)
        future.add_done_callback(lambda done: self.events.put(("done", done, complete)))

    # Process only signaled events on Tk's thread; a virtual event wakes us when work arrives.
    def _drain_events(self):
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "status":
                if not self.closing:
                    self._status(event[1])
            elif event[0] == "done":
                self.busy = False
                try:
                    value = event[1].result()
                    event[2](value)
                except Exception as exc:
                    self.watch_var.set(self.watcher is not None)
                    self._clear_preview()
                    self._status(f"Action stopped: {exc}")
                    if not self.closing:
                        self._show_window()
                        messagebox.showerror("DeskSweep — action stopped", str(exc), parent=self)
                self._refresh_controls()
            elif event[0] == "closed":
                try:
                    event[1].result()
                except Exception as exc:
                    self._status(f"Shutdown needs attention: {exc}")
                    self.closing = False
                    self._refresh_controls()
                    continue
                self.executor.shutdown(wait=False)
                self.events.close()
                for timer in self.tk.call("after", "info"):
                    self.after_cancel(timer)
                self.destroy()
                return
            elif event[0] == "tray_ready":
                self._tray_ready = True
                self._refresh_controls()
                if self._hide_when_ready and not self.closing:
                    self._minimize_to_tray()
            elif event[0] == "tray_open" and not self.closing:
                self._show_window()
            elif event[0] == "tray_toggle" and not self.closing and not self.busy:
                if self.config_data:
                    self.watch_var.set(self.watcher is None)
                    if self.watcher is None and not self._selected_folders():
                        self._show_window()
                    self._toggle_watch()
            elif event[0] == "tray_exit":
                self._quit()
            elif event[0] == "tray_error":
                self._tray_error = event[1]
                self._tray_ready = False
                if not self.closing:
                    self._show_window()
                    self._status(f"Tray unavailable: {event[1]}. The window will remain open.")

    # Clear both visible and stored plans so buttons cannot execute a stale preview.
    def _clear_preview(self, text=None):
        self._render_token += 1
        self.planned_moves = []
        children = self.table.get_children()
        if children:
            self.table.delete(*children)
        self.count_label.configure(text="0 items")
        self.empty_label.configure(text=text or "Ready for a fresh preview.\n\nSelect folders, then click Scan & Preview.")
        self.empty_label.place(relx=0.5, rely=0.53, anchor="center")

    # Scan only the captured selection and leave the disk unchanged until Organize Now is clicked.
    def _scan(self):
        folders = self._selected_folders()
        if not folders:
            self._status("Select at least one folder first.")
            return
        config = self._effective_config()
        self._clear_preview()
        self._submit(lambda: scan_folders(folders, config), self._show_preview, "Preparing your move preview…")

    # Render rows in small chunks so even a large preview keeps the window responsive.
    def _show_preview(self, moves):
        self.planned_moves = moves
        self.count_label.configure(text=f"{len(moves):,} items")
        if moves:
            self.empty_label.place_forget()
            self._render_rows(moves, 0, self._render_token)
            self._status(f"Preview ready: {len(moves):,} items. Nothing has moved yet.")
        else:
            self.empty_label.configure(text="All clear.\n\nNo eligible top-level items in these folders.\nHidden items and category folders stay put.")
            self._status("All clear. Nothing needs organizing in these folders.")

    # Yield between batches of table rows and cancel old rendering when the preview changes.
    def _render_rows(self, moves, offset, token):
        if token != self._render_token or self.closing:
            return
        for move in moves[offset:offset + 150]:
            self.table.insert("", "end", values=(move["filename"], move["category"], move["target_path"]))
        if offset + 150 < len(moves):
            self.after(1, self._render_rows, moves, offset + 150, token)

    # Execute exactly the previewed source set; the core rechecks identity and filename collisions.
    def _organize(self):
        moves = list(self.planned_moves)
        self._submit(lambda: execute_moves(moves, HISTORY_FILE), self._organized, "Organizing items and recording undo history…")

    # Summarize success and expose per-file failures without leaving a reusable stale plan.
    def _organized(self, result):
        self._clear_preview("Sweep complete.\n\nScan again for a fresh preview, or undo the last run.")
        self._report_result(result, "moved", "Organized")

    # Stop automatic sorting before undo so restored files are not immediately organized again.
    def _undo(self):
        self._submit(self._undo_work, self._undone, "Stopping Auto-Watch and restoring the last batch…")

    # Perform watcher shutdown and disk restoration together on the serialized action worker.
    def _undo_work(self):
        self._stop_watcher()
        return undo_last_batch(HISTORY_FILE)

    # Refresh the UI after undo and leave watching off for deliberate user reactivation.
    def _undone(self, result):
        self.watch_var.set(False)
        self._clear_preview("Undo finished.\n\nScan again when you are ready. Auto-Watch is off.")
        self._report_result(result, "restored", "Restored")

    # Put counts in the status bar and show actionable details only when some files need attention.
    def _report_result(self, result, key, verb):
        count, errors = len(result[key]), result["errors"]
        message = f"{verb} {count} item(s)."
        if result["batch_id"] is None:
            message = "Nothing to undo yet. Your next sweep will appear here." if key == "restored" else "All clear. There is nothing to move right now."
        if errors:
            message += f" {len(errors)} item(s) need attention."
            details = "\n\n".join(f"{e['source_path']}\n{e['error']}" for e in errors[:12])
            if len(errors) > 12:
                details += f"\n\n…and {len(errors) - 12} more. Rescan or retry undo after fixing the conflicts."
            if not self.closing:
                messagebox.showwarning("DeskSweep — some items stayed put", details, parent=self)
        self._status(message)

    # Start or stop watching explicitly, capturing all Tk values before dispatching worker work.
    def _toggle_watch(self):
        if self.watch_var.get():
            folders = self._selected_folders()
            if not folders:
                self.watch_var.set(False)
                self._status("Select at least one folder before enabling Auto-Watch.")
                return
            config = self._effective_config()
            self._clear_preview("Auto-Watch is on.\n\nNew arrivals are sorted after settling.\nTurn it off to preview files manually.")
            self._submit(lambda: self._start_watcher(folders, config), self._watch_started, "Starting Auto-Watch…")
        else:
            self._submit(self._stop_watcher, self._watch_stopped, "Stopping Auto-Watch…")

    # Create a watcher with queue-only notifications so its threads never touch GUI widgets.
    def _start_watcher(self, folders, config):
        watcher = FolderWatcher(folders, config, HISTORY_FILE,
                                callback=lambda text: self.events.put(("status", text)),
                                settle_seconds=config["settle_seconds"])
        watcher.start()
        self.watcher = watcher

    # Confirm that automatic sorting is live and explain its new-arrival-only scope.
    def _watch_started(self, unused):
        self.watch_var.set(True)
        self._status("Auto-Watch is on. Only new or renamed files will be organized.")

    # Join the watcher outside Tk so shutdown does not freeze the interface.
    def _stop_watcher(self):
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None

    # Reset the preview after watching stops because disk contents may have changed.
    def _watch_stopped(self, unused):
        self.watch_var.set(False)
        self._clear_preview()
        self._status("Auto-Watch is off. Scan for a fresh preview.")

    # Offer a focused JSON editor with validation so rules stay transparent and easy to learn.
    def _edit_rules(self):
        if self.rules_window is not None and self.rules_window.winfo_exists():
            self.rules_window.focus()
            return
        modal = EventDrivenToplevel(self)
        self.rules_window = modal
        modal.title("DeskSweep — Edit Rules")
        modal.geometry("690x610")
        modal.minsize(540, 430)
        modal.transient(self)
        modal.grab_set()
        modal.grid_columnconfigure(0, weight=1)
        modal.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(modal, text="Make these rules your own. For example, Documents collects .pdf and .doc files.\nsettle_seconds controls the quiet period before new arrivals are sorted.",
                     justify="left", text_color=THEME["muted"]).grid(row=0, column=0, sticky="w", padx=22, pady=18)
        editor = EventDrivenTextbox(modal, font=("Consolas", 13), wrap="none")
        editor.grid(row=1, column=0, sticky="nsew", padx=22)
        try:
            editor.insert("1.0", CONFIG_FILE.read_text(encoding="utf-8"))
        except OSError:
            editor.insert("1.0", json.dumps({"categories": {"Others": []}, "ignored_extensions": sorted([".crdownload", ".part", ".tmp", ".ini", ".lnk"])}, indent=2))
        error_label = ctk.CTkLabel(modal, text="", text_color=THEME["error"], wraplength=630)
        error_label.grid(row=2, column=0, sticky="w", padx=22, pady=10)
        ctk.CTkButton(modal, text="Save Rules", command=lambda: self._save_rules(modal, editor, error_label)).grid(row=3, column=0, sticky="e", padx=22, pady=(0, 20))

    # Validate before saving atomically, then discard any preview based on the old rules.
    def _save_rules(self, modal, editor, error_label):
        try:
            config = validate_config(json.loads(editor.get("1.0", "end")))
            destination_directory(config)
            atomic_write_json(CONFIG_FILE, config)
        except (OSError, ValueError) as exc:
            error_label.configure(text=str(exc))
            return
        options_changed = any(config[key] != (self.config_data or {}).get(key) for key in
                              ("destination_root", "organize_folders", "folders_category_name"))
        self.config_data = config
        self.settle_var.set(f"{config['settle_seconds']:g}")
        modal.destroy()
        self.rules_window = None
        if options_changed:
            self._settings_saved(config)
            return
        self._clear_preview()
        self._status("Rules saved. Scan again to use them.")
        self._refresh_controls()

    # Close to the tray while watching; otherwise preserve the familiar close-to-quit behavior.
    def _close(self):
        if self.closing:
            return
        if self.watcher is not None or self.watch_var.get():
            self._minimize_to_tray()
        else:
            self._quit()

    # Hide only after the tray is usable so a startup failure cannot strand the application.
    def _minimize_to_tray(self):
        if self._tray_error:
            self._show_window()
            self._status(f"Tray unavailable: {self._tray_error}. Turn off Auto-Watch to close normally.")
            return
        self._hide_when_ready = True
        if self.tray is None:
            try:
                self.tray = TrayController(self.events.put)
                self.tray.update_state(self.watcher is not None, not self.busy)
                self.tray.start()
            except Exception as exc:
                self._tray_error = str(exc)
                self._show_window()
                self._status(f"Tray unavailable: {exc}. The window will remain open.")
                return
        if self._tray_ready:
            if self.rules_window is not None and self.rules_window.winfo_exists():
                self.rules_window.grab_release()
                self.rules_window.withdraw()
            self._hide_when_ready = False
            self._hidden_to_tray = True
            self.withdraw()
            self._status("DeskSweep is running in the tray. Right-click its icon to open or quit.")

    # Restore the existing window and any editor rather than creating a second application instance.
    def _show_window(self):
        self._hide_when_ready = False
        self._hidden_to_tray = False
        self.deiconify()
        self.lift()
        self.focus_force()
        if self.rules_window is not None and self.rules_window.winfo_exists():
            self.rules_window.deiconify()
            self.rules_window.grab_set()

    # Stop producers on the action worker while Tk continues handling completion messages.
    def _shutdown_services(self):
        self._stop_watcher()
        if self.tray is not None:
            self.tray.stop()

    # Queue real shutdown behind current work; the tray Quit command always takes this path.
    def _quit(self):
        if self.closing:
            return
        self.closing = True
        self._status("Finishing the current operation and stopping Auto-Watch…")
        self._refresh_controls()
        future = self.executor.submit(self._shutdown_services)
        future.add_done_callback(lambda done: self.events.put(("closed", done)))


# Set the application theme once and enter Tk's main event loop.
def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = DeskSweepApp()
    app.mainloop()


if __name__ == "__main__":
    main()
