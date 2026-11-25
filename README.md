# DeskSweep v1.1

A small Windows 11 desktop file organizer built with Python and CustomTkinter. Preview where files will go, organize without overwriting duplicates, and undo the latest batch. Auto-Watch is optional and off by default.

## Setup and launch

Use a standard Windows Python 3.11 or newer installation with Tk support (the installer from [python.org](https://www.python.org/downloads/windows/) includes it). In PowerShell:

```powershell
cd E:\chuma\.vscode\DeskSweep
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

No environment activation or PowerShell execution-policy change is needed. For a different location, change the first path.

**On this workspace:** the standard Python installation is missing its Tcl/Tk runtime files. A separate `.venv-tk` environment uses the working Python already installed on this machine. Launch the verified local environment with:

```powershell
cd E:\chuma\.vscode\DeskSweep
.\.venv-tk\bin\python.exe main.py
```

Use `.\.venv-tk\bin\python.exe` in place of `.\.venv\Scripts\python.exe` for tests here. Other Windows machines with a complete standard Python installation can use the normal setup above.

`pystray` and Pillow are installed in `.venv-tk`. This MSYS Python required a local Pillow build using its zlib headers; unused JPEG support is disabled in that local build. PNG/ICO drawing and tray rendering are verified. Standard Windows Python installations use Pillow's prebuilt wheel with the usual `pip install -r requirements.txt` command.

Choose Downloads, Desktop, or **Add a folder**. Both default choices start unchecked. These defaults use `Path.home()`; use the folder picker if Windows redirects your Desktop to OneDrive or another location.

1. **Scan & Preview** reads top-level files and displays their full destinations. It makes no disk changes.
2. **Organize Now** moves the previewed files into category subfolders inside each source directory, or inside your custom destination.
3. **Undo Last Run** restores the most recent unfinished batch to its original paths. Click again to undo an earlier batch after the latest one is fully restored.

The **Output Destination** row initially shows **Same as source folder (Default)**. Click **Browse...** to choose an existing output folder; DeskSweep saves it as `destination_root` in `config.json` and automatically rescans the selected sources. A PDF then goes to `<destination>\Documents\filename.pdf`. **Reset** clears the setting to `""` and refreshes the preview for in-place sorting. Destination controls are disabled while work or Auto-Watch is running; stop watching before changing the output folder.

An unavailable, relative, or invalid destination produces an error instead of silently falling back to in-place sorting. Files already inside the destination root or any of its subfolders are excluded, including when the source and destination are the same folder. Choose **Reset** if you want in-place sorting there. Undo uses the paths recorded for the batch, even if you later change the destination.

**Group existing folders into /Folders** is off by default. Enable it to include loose top-level folders in the preview, alongside files. The switch saves `organize_folders` and immediately refreshes the preview; only **Organize Now** moves anything. A folder such as `Lectures` goes to `<output base>\Folders\Lectures`, keeping all its contents together. `folders_category_name` defaults to `"Folders"` and can be changed in **Edit Rules**; the switch label follows that name.

Grouping skips hidden/system folders, links/junctions, excluded paths, all standard and configured category names (case-insensitively), and the destination root. Folders containing the destination, excluded data, or another explicitly selected source are also kept in place. Duplicate folder names become `Lectures (1)` or `Project.v1 (1)`, with no merging into existing folders. Undo restores the whole folder to its original path, including edits made inside it since organizing; an occupied original path is reported as a conflict. The grouping switch is disabled during work and Auto-Watch. Auto-Watch continues to handle new **files** only; grouping existing folders requires the manual preview workflow.

## Adjustable safety pause

`settle_seconds` defaults to `3.0`. It must be a finite number greater than zero; fractional seconds are supported, while booleans, strings, zero, negative values, NaN, and infinity are rejected. Old configurations automatically receive the default when loaded.

Change **Safety pause (seconds)** and click **Apply**, even while Auto-Watch is running. DeskSweep saves the value and adjusts queued quiet-period deadlines without restarting the watcher. Increasing it extends the wait; decreasing it can release an already quiet file sooner. A file move already in progress finishes normally. You can also edit this setting through **Edit Rules** while watching is off. Direct edits to `config.json` take effect on the next app launch; no configuration polling is added.

The v1.1 interface uses a shared Windows 11 Charcoal & Indigo `THEME` palette in `main.py`.

## Running in the system tray

- With **Auto-Watch on**, clicking **X** hides DeskSweep after its folder-and-broom tray icon is ready. Watching continues. Windows may place the icon in the taskbar's hidden-icons overflow.
- Right-click the icon for **Open DeskSweep**, **Toggle Auto-Watch (On/Off)**, and **Quit**. Clicking the icon's default action also restores the existing window. The watcher toggle is disabled during an action or shutdown.
- Turning watching off from the tray leaves the icon available so you can turn it back on, open the window, or quit. With Auto-Watch off, clicking the window's **X** quits normally.
- **Quit** always quits, even while watching. It finishes the current action, stops and joins the watcher and tray threads, then destroys the window. Tray startup failures leave the window accessible.

The icon is drawn in memory with Pillow; no bundled `.ico` asset is required. Idle work is event-driven: Tk virtual events deliver worker/tray notifications, Windows filesystem notifications wake watchdog, and a condition variable waits until a pending file's settling/retry deadline. With no pending files, the sorter waits indefinitely. CustomTkinter's theme/DPI polling is suppressed for the fixed dark theme; monitor scaling and rule-editor scrollbars update on window/content events. One-shot startup, settling, and rendering callbacks remain. Active file copying and rendering still consume CPU; exact idle usage depends on the machine.

## Try a disposable folder first

This creates uniquely named test data in your temporary directory, without touching your real Desktop or Downloads:

```powershell
$demoFolder = Join-Path $env:TEMP ('DeskSweep-demo-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $demoFolder | Out-Null
New-Item -ItemType Directory -Path (Join-Path $demoFolder 'Documents') | Out-Null
New-Item -ItemType Directory -Path (Join-Path $demoFolder 'ExistingFolder') | Out-Null
Set-Content -LiteralPath (Join-Path $demoFolder 'lecture.pdf') -Value 'Dummy PDF placeholder'
Set-Content -LiteralPath (Join-Path $demoFolder 'Documents\lecture.pdf') -Value 'Existing duplicate: keep me'
Set-Content -LiteralPath (Join-Path $demoFolder 'exercise.py') -Value 'print("hello")'
Set-Content -LiteralPath (Join-Path $demoFolder 'screenshot.png') -Value 'Dummy image placeholder'
Set-Content -LiteralPath (Join-Path $demoFolder 'unfinished.crdownload') -Value 'Leave me alone'
Set-Content -LiteralPath (Join-Path $demoFolder 'ExistingFolder\nested.txt') -Value 'Leave me alone too'
$demoFolder
```

The PDF/image fixtures are plain text placeholders; classification uses their extensions.

- Launch DeskSweep, keep **Downloads**, **Desktop**, and folder grouping unchecked, reset the destination to **Same as source folder (Default)**, and add only the printed folder.
- Scan: expect three planned moves, including `Documents\lecture (1).pdf`. The incomplete download and nested file should not appear.
- Organize: confirm the existing `Documents\lecture.pdf` is unchanged.
- Undo: the three files return to the root. Newly created empty `Code` and `Images` directories disappear; the pre-existing `Documents` directory remains.
- To test undo conflicts, organize again, create another `lecture.pdf` in the root, then undo. DeskSweep leaves that file alone and reports the conflict. Move your new file aside and click undo again to finish the same batch.
- To test Auto-Watch, enable it for this demo folder, then create `new.pdf` there. It should move after the configured quiet period (three seconds by default). Existing root files are not swept when watching starts. Undo stops watching first and leaves it off.
- To test a custom destination, turn Auto-Watch off and choose a separate empty test folder with **Browse...**. Check that the preview updates to that folder, organize, and undo. The original files return to the demo folder, and the chosen destination root remains. Click **Reset** to return to in-place sorting.
- To test folder grouping after undo, enable **Group existing folders into /Folders**. The refreshed preview should include `ExistingFolder` → `Folders\ExistingFolder`, while `Documents` stays put. Organize, check that `nested.txt` stays inside the moved folder, then undo. `ExistingFolder\nested.txt` returns to its original path, and the newly created empty `Folders` directory is removed.

## Files and architecture

```text
DeskSweep/
├── config.json           # Category extensions and ignored suffixes
├── organizer.py          # Standard-library planning, journal, execution, undo
├── watcher.py            # Watchdog observer + settling worker
├── tray.py               # Pillow artwork + pystray menu and lifecycle
├── ui_events.py          # Tk event delivery + UI without recurring polling
├── main.py               # CustomTkinter GUI + serialized action worker
├── requirements.txt      # CustomTkinter, watchdog, pystray, Pillow
├── README.md
└── tests/
    ├── test_folders.py    # Folder exclusions, duplicates, transfers, and undo
    ├── test_organizer.py  # Filesystem safety and recovery regression tests
    ├── test_watcher.py    # Real filesystem-event integration tests
    ├── test_tray.py       # Native tray lifecycle and idle event delivery
    └── test_gui.py        # Hidden-window UI workflow smoke test
```

At runtime, `history.json` stores versioned, timestamped batches beside the app. A small `history.json.lock` file serializes writers across threads and DeskSweep instances sharing that journal. The GUI uses absolute app-relative config/history paths, even if launched from another working directory. Direct core API calls default to `history.json` in the caller's working directory.

- **`config.json`** maps categories to case-insensitive, single file suffixes. Unmatched files go to `Others`. Edit the JSON directly or use **Edit Rules**, which validates before saving and clears the old preview. Duplicate extension assignments, unsafe folder names, and removal of `Others` are rejected. Required ignores (`.crdownload`, `.part`, `.tmp`, `.ini`, `.lnk`) cannot be disabled. Optional `excluded_paths` accepts a list of absolute file paths to skip. The GUI automatically excludes its own application files and journal.
- **`organizer.py`** exposes `scan_folders(folder_paths, config)`, `execute_moves(planned_moves, history_file='history.json')`, and `undo_last_batch(history_file='history.json')`. Plans contain `filename`, `source_path`, `category`, `target_path`, and a file-identity snapshot. Execute returns `moved`, `errors`, and `batch_id`; undo returns `restored`, `errors`, and `batch_id`. Individual failures are reported while safe files continue. Journal-level failures stop the operation.
- **`watcher.py`** listens for creation/rename events without recursion, debounces modification events, checks size/mtime stability, and retries locked files up to five times. It organizes only new arrivals. Overlapping watched roots are rejected to avoid repeated sorting into nested category folders. Each automatic file move is its own undo batch. Disable watching to change folders or rules.
- **`main.py`** owns all Tk widgets on the main thread. Scans, moves, undo, watcher start/stop, and shutdown run in a single action executor. Worker notifications enter a queue and generate a Tk virtual event; there is no recurring queue check. Preview rows are rendered in chunks, and the table scrolls horizontally and vertically. The close button chooses tray hiding or real shutdown based on Auto-Watch state.
- **`tray.py`** draws the icon in memory and runs Windows' blocking tray message loop on its own thread. Menu callbacks only send queued requests; menu labels/checkmarks read a synchronized state snapshot instead of Tk variables. **`ui_events.py`** delivers those requests through Tk's thread-aware event system and replaces CustomTkinter's recurring theme, DPI, and textbox checks with event handlers. These adapters are tested against the dependency versions installed here; review them when upgrading major versions.

The implementation uses the documented [CustomTkinter widgets](https://customtkinter.tomschimansky.com/documentation/widgets/) and [watchdog observer lifecycle](https://python-watchdog.readthedocs.io/en/stable/quickstart.html).

## Safety behavior and limits

- Scans classify top-level entries only. Dotfiles, Windows hidden/system entries, symlinks, junctions, and temporary file extensions are excluded. Linked/junction directory paths are rejected. Existing subfolders move as whole units only when grouping is enabled; their contents are not individually categorized. Category folders are never grouped.
- Destination collisions become `name (1).ext`, `name (2).ext`, etc. Names are checked case-insensitively. Destinations are rechecked during execution, so a collision appearing after preview may change the final name. Windows' no-replace rename still refuses to overwrite a file that appears in the final instant.
- Moves on the same drive use Windows' atomic no-replace rename. For a destination on another drive, DeskSweep copies to a temporary file, flushes it, records its new identity in history, publishes it without overwriting, and only then removes the source. Undo uses the same protection in reverse. The non-Windows fallback requires hard-link support on the destination filesystem. Windows 11 is the target platform.
- Cross-drive folder moves use a staged copy and recorded tree identities. Cleanup removes only verified original entries; changed or newly added contents are left alone. A cleanup interruption can leave a complete destination copy and a partial original; resolve the reported conflict manually rather than discarding either copy. Cross-drive folder trees containing links/junctions are rejected before copying. Directory moves require Windows; the core's non-Windows file support remains available.
- History records intent **before** each move and is flushed and atomically replaced after state changes. An interrupted rename can be reconciled using file identity on the next organize/undo action. If history cannot be read or written, the batch stops. Keep `history.json` until you no longer need its undo records; do not edit it while the app is running.
- Undo never overwrites an occupied original path. It skips missing, replaced, or edited organized files and leaves unresolved entries retryable. Resolve conflicts manually and retry. It only removes empty category directories recorded as created by that batch. Existing category folders remain.
- The configurable quiet period (three seconds by default) is a practical download heuristic, not proof that every downloader has finished. Programs can pause writing for longer or keep files open; use manual preview for sensitive folders. A damaged disk, lost history, external file changes, or a power failure can still require manual recovery. This tool is not a backup system.
- Auto-Watch runs while the window is visible or DeskSweep is in the tray. **Quit** stops it. There is no login startup task or Windows service. An unavailable watched drive or event-delivery failure may require turning watching off/on and doing a manual scan.

## Verification

From the project directory, after installing requirements:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

All tests create disposable directories; none use your real Downloads or Desktop. Core tests cover dry-run immutability, exclusions, collisions, stale previews, partial moves, corrupt/unwritable history, interrupted journal writes, rollback conflicts, exact restoration, and empty-folder cleanup. Folder tests cover opt-in grouping, category exclusions, custom names/destinations, directory collisions, and cross-drive recovery. Watcher tests exercise real creation/rename/modification events and thread shutdown. The GUI smoke test verifies the grouping switch, persistence, preview refresh, and undo using a withdrawn window and temporary config/history/data.

The v1.1 suite also checks positive finite settle-delay validation, live delay changes and failed saves, Windows hidden/system folders, excluded ancestors, and close-to-tray versus close-to-quit behavior. Tray checks exercise the real native icon and verify there are no recurring idle Tk callbacks.

You can run the standard-library-only safety tests without the GUI dependencies:

```powershell
py -3 -m unittest discover -s tests -p test_organizer.py -v
```
