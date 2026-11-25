"""Standard-library-only planning, journaled moves, and conflict-safe undo."""

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from uuid import uuid4

MANDATORY_IGNORES = {".crdownload", ".part", ".tmp", ".ini", ".lnk"}
STANDARD_CATEGORIES = {"Documents", "Code", "Images", "Archives", "Installers", "Media", "Others", "Folders"}
_LOCK = threading.RLock()
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


# Validate category names so a rule cannot send files outside the selected folder.
def _category_name(name):
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or name.startswith(".") or name != name.strip() or name.endswith(".")
            or any(c in name for c in '<>:"/\\|?*')
            or any(ord(c) < 32 for c in name)
            or name.split(".")[0].upper() in _RESERVED):
        raise ValueError(f"Invalid Windows category folder name: {name!r}")
    return name


# Normalize user rules and reject ambiguity before any file operations happen.
def validate_config(config):
    if not isinstance(config, dict) or not isinstance(config.get("categories"), dict):
        raise ValueError('Rules need a "categories" object.')
    categories, used, names = {}, set(), set()
    for category, extensions in config["categories"].items():
        _category_name(category)
        if category.casefold() in names:
            raise ValueError("Category names must be unique, ignoring case.")
        names.add(category.casefold())
        if not isinstance(extensions, list):
            raise ValueError(f"{category}: extensions must be a list.")
        categories[category] = []
        for ext in extensions:
            ext = _extension(ext)
            if ext in used:
                raise ValueError(f"Extension appears more than once: {ext}")
            used.add(ext)
            categories[category].append(ext)
    if "Others" not in categories:
        raise ValueError('Keep an "Others" category for unmatched files.')
    ignored = config.get("ignored_extensions", [])
    if not isinstance(ignored, list):
        raise ValueError("ignored_extensions must be a list.")
    excluded = config.get("excluded_paths", [])
    if not isinstance(excluded, list) or not all(isinstance(p, str) for p in excluded):
        raise ValueError("excluded_paths must be a list of file paths.")
    destination = config.get("destination_root", "")
    if not isinstance(destination, str):
        raise ValueError("destination_root must be a folder path or an empty string.")
    if destination:
        destination = Path(destination).expanduser()
        if not destination.is_absolute() or ".." in destination.parts:
            raise ValueError("destination_root must be an absolute folder path without '..'.")
        destination = str(destination)
    organize_folders = config.get("organize_folders", False)
    if not isinstance(organize_folders, bool):
        raise ValueError("organize_folders must be true or false.")
    folders_category = _category_name(config.get("folders_category_name", "Folders"))
    settle_seconds = validate_settle_seconds(config.get("settle_seconds", 3.0))
    return {"categories": categories,
            "ignored_extensions": sorted(MANDATORY_IGNORES | {_extension(e) for e in ignored}),
            "excluded_paths": list(excluded), "destination_root": destination,
            "organize_folders": organize_folders, "folders_category_name": folders_category,
            "settle_seconds": settle_seconds}


# Require a finite positive delay so invalid settings cannot trigger tight loops or endless waits.
def validate_settle_seconds(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError
        return float(value)
    except (ValueError, OverflowError):
        raise ValueError("settle_seconds must be a finite positive number.") from None


# Validate the selected output folder before scanning or saving, without silently falling back.
def destination_directory(config):
    raw = validate_config(config)["destination_root"]
    if not raw:
        return None
    destination = Path(raw)
    _safe_directory(destination)
    return destination


# Accept single suffixes only so matching remains easy to understand and edit.
def _extension(value):
    if (not isinstance(value, str) or len(value) < 2 or not value.startswith(".")
            or any(c in value[1:] for c in '. /\\:*?"<>|') or not value.isprintable()):
        raise ValueError(f"Invalid extension: {value!r}; use a suffix such as .pdf.")
    return value.lower()


# Read rules through one validator so the GUI and watcher interpret them identically.
def load_config(path="config.json"):
    return validate_config(json.loads(Path(path).read_text(encoding="utf-8")))


# Replace JSON atomically so an interrupted write cannot leave a half-written journal.
def atomic_write_json(path, data):
    path = Path(path).absolute()
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temp_name = stream.name
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


# Detect Windows hidden/system/reparse entries as well as dotfiles and symbolic links.
def _unsafe_entry(path):
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    return (path.name.startswith(".") or path.is_symlink()
            or bool(attributes & (0x2 | 0x4 | 0x400)))


# Reject linked directory components to keep moves within the actual chosen directory.
def _safe_directory(path):
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Linked folders and junctions are not supported: {part}")
    if not path.is_dir():
        raise NotADirectoryError(str(path))


# Identify files and whole directories so replacement items cannot be moved from a stale preview.
def _signature(path):
    info = path.lstat()
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or _unsafe_entry(path):
        raise ValueError(f"Not a visible regular file or directory: {path}")
    return _identity(path)


# Read identity for our hidden staging files too, so cross-drive copies can be journaled safely.
def _identity(path):
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        return [info.st_dev, info.st_ino, -1, 0]  # Directory identity survives edits to its contents.
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


# Compare journal identities conservatively so undo leaves edited or replaced files alone.
def _matches(path, signature):
    try:
        return _signature(path) == signature
    except (OSError, ValueError):
        return False


# Reserve a readable filename in memory while checking existing files, including case clashes.
def _unique_target(target, reserved=None, is_directory=False):
    reserved = reserved if reserved is not None else set()
    existing = {p.name.casefold() for p in target.parent.iterdir()} if target.parent.exists() else set()
    candidate, index = target, 1
    while candidate.name.casefold() in existing or str(candidate).casefold() in reserved:
        candidate = target.with_name(f"{target.name} ({index})" if is_directory
                                     else f"{target.stem} ({index}){target.suffix}")
        index += 1
    reserved.add(str(candidate).casefold())
    return candidate


# Build a read-only top-level plan; no folders, files, or history are created during preview.
def scan_folders(folder_paths, config):
    config = validate_config(config)
    destination_root = destination_directory(config)
    mapping = {ext: category for category, exts in config["categories"].items() for ext in exts}
    ignored = set(config["ignored_extensions"])
    excluded_paths = [Path(p).expanduser().absolute() for p in config["excluded_paths"]]
    folders = list(dict.fromkeys(Path(raw).expanduser().absolute() for raw in folder_paths))
    category_names = {name.casefold() for name in
                      STANDARD_CATEGORIES | set(config["categories"]) | {config["folders_category_name"]}}
    planned, reserved, seen = [], set(), set()
    for folder in folders:
        _safe_directory(folder)
        if destination_root is not None and folder.is_relative_to(destination_root):
            continue  # Already inside the output tree: never organize these files again.
        key = str(folder).casefold()
        if key in seen:
            continue
        seen.add(key)
        for source in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            try:
                if any(source.is_relative_to(path) for path in excluded_paths) or _unsafe_entry(source):
                    continue
                is_directory = source.is_dir()
                if is_directory:
                    if (not config["organize_folders"] or source.name.casefold() in category_names
                            or any(path.is_relative_to(source) for path in excluded_paths)
                            or any(path.is_relative_to(source) for path in folders)
                            or (destination_root is not None and
                                (source.is_relative_to(destination_root) or destination_root.is_relative_to(source)))):
                        continue
                    category = config["folders_category_name"]
                else:
                    if not source.is_file() or source.suffix.lower() in ignored:
                        continue
                    category = mapping.get(source.suffix.lower(), "Others")
                destination = (destination_root or folder) / category
                if destination.exists() or destination.is_symlink():
                    _safe_directory(destination)
                    if _unsafe_entry(destination):
                        raise ValueError(f"Category folder is hidden or unsafe: {destination}")
                target = _unique_target(destination / source.name, reserved, is_directory)
                planned.append({"filename": source.name, "source_path": str(source),
                                "category": category, "target_path": str(target),
                                "signature": _signature(source),
                                "item_type": "directory" if is_directory else "file",
                                "destination_root": str(destination_root) if destination_root else ""})
            except FileNotFoundError:
                continue  # A download may disappear while the directory is being scanned.
    return planned


# Serialize threads and app instances sharing a history file so their batches cannot collide.
@contextmanager
def _history_lock(path):
    with _LOCK:
        with open(str(path) + ".lock", "a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# Validate journal structure before trusting it; corruption must stop writes, never erase history.
def _read_history(path):
    if not path.exists():
        return {"version": 1, "batches": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("batches"), list):
        raise ValueError("Unsupported or damaged history.json; keep it for manual recovery.")
    for batch in data["batches"]:
        if not isinstance(batch, dict) or not isinstance(batch.get("moves"), list) or not isinstance(batch.get("created_dirs"), list):
            raise ValueError("Damaged history batch; no files were moved.")
        for move in batch["moves"]:
            _validate_move(move)
            if move.get("state") not in {"pending", "moved", "undo_pending", "undone", "failed"}:
                raise ValueError("Unknown history move state.")
            if not isinstance(move.get("signature"), list) or len(move["signature"]) != 4:
                raise ValueError("Missing file identity in history.")
        allowed = {str(Path(m["target_path"]).parent) for m in batch["moves"]}
        if any(directory not in allowed for directory in batch["created_dirs"]):
            raise ValueError("Unexpected folder in history cleanup list.")
    return data


# Check the plan's boundaries again at execution and undo time to catch tampered paths.
def _validate_move(move):
    if not isinstance(move, dict) or not all(isinstance(move.get(key), str)
                                            for key in ("source_path", "target_path", "category")):
        raise ValueError("A move needs source_path, target_path, and category strings.")
    source, target = Path(move["source_path"]), Path(move["target_path"])
    category = _category_name(move["category"])
    raw_root = move.get("destination_root", "")
    if not isinstance(raw_root, str):
        raise ValueError("Invalid destination_root in move.")
    root = Path(raw_root) if raw_root else source.parent
    item_type = move.get("item_type", "file")
    if item_type not in {"file", "directory"}:
        raise ValueError("Invalid item_type in move.")
    if (not source.is_absolute() or not target.is_absolute()
            or not root.is_absolute() or ".." in root.parts
            or ".." in source.parts or ".." in target.parts
            or target.parent != root / category or source == target
            or (raw_root and source.is_relative_to(root))
            or (item_type == "file" and source.suffix.lower() in MANDATORY_IGNORES)
            or (item_type == "directory" and
                (target.is_relative_to(source) or source.name.casefold() in
                 {name.casefold() for name in STANDARD_CATEGORIES | {category}}))):
        raise ValueError("A move must go into a category inside its recorded output folder.")
    return source, target


# Use an atomic no-replace rename on Windows; other systems use a no-replace hard link.
def _move_no_replace(source, target):
    if os.name == "nt":
        os.rename(source, target)  # Windows raises FileExistsError instead of overwriting.
    else:
        if source.is_dir():
            raise OSError("Safe directory renames currently require Windows.")
        os.link(source, target, follow_symlinks=False)
        source.unlink()


# Stage cross-drive copies and record their new identity before publishing or removing the source.
def _move_journaled(source, target, entry, history, history_file, identity_key):
    expected = (entry["signature"] if identity_key == "target_signature"
                else entry.get("target_signature", entry["signature"]))
    if _signature(source) != expected:
        raise ValueError("File changed before moving; left untouched.")
    try:
        _move_no_replace(source, target)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
            raise
    if entry.get("item_type") == "directory":
        _copy_directory_journaled(source, target, entry, history, history_file, identity_key)
        return
    descriptor, name = tempfile.mkstemp(prefix=".desksweep-", suffix=".tmp", dir=target.parent)
    staging = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, staging)
        if _signature(source) != expected:
            raise ValueError("File changed while copying; source left untouched.")
        entry[identity_key] = _identity(staging)
        atomic_write_json(history_file, history)
        _safe_directory(target.parent)
        _move_no_replace(staging, target)
        if _signature(source) != expected:
            raise ValueError("Source changed after copying; both copies kept for recovery.")
        source.unlink()
    finally:
        staging.unlink(missing_ok=True)


# Snapshot a tree without following links so cross-drive cleanup can verify every item it removes.
def _tree_snapshot(root):
    snapshot, pending = {}, [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Cross-drive folder moves cannot contain links or junctions: {path}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError(f"Unsupported item inside folder: {path}")
        snapshot[str(path.relative_to(root))] = _identity(path)
        if stat.S_ISDIR(info.st_mode):
            pending.extend(sorted(path.iterdir(), reverse=True))
    return snapshot


# Match a complete copied tree before discarding a duplicate during recovery.
def _tree_matches(path, snapshot):
    try:
        return snapshot is not None and _tree_snapshot(path) == snapshot
    except (OSError, ValueError):
        return False


# Delete only journal-verified entries; new or edited contents and nonempty directories stay intact.
def _remove_tree_verified(root, snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("Missing folder-copy record; leave both locations for manual recovery.")
    current = _tree_snapshot(root)
    if any(snapshot.get(relative) != identity for relative, identity in current.items()):
        raise ValueError(f"Folder changed during transfer; both locations kept for recovery: {root}")
    for relative in sorted(current, key=lambda name: len(Path(name).parts), reverse=True):
        path = root / relative
        _safe_directory(path.parent)
        if _identity(path) != current[relative]:
            raise ValueError(f"Item changed during cleanup; left untouched: {path}")
        if current[relative][2] == -1:
            path.rmdir()
        else:
            path.unlink()


# Copy a directory across drives, record both trees, and publish before removing any original data.
def _copy_directory_journaled(source, target, entry, history, history_file, identity_key):
    before = _tree_snapshot(source)
    staging = Path(tempfile.mkdtemp(prefix=".desksweep-", suffix=".tmp", dir=target.parent))
    tree_key = "target_tree" if identity_key == "target_signature" else "restored_tree"
    source_key = "source_tree" if identity_key == "target_signature" else "undo_source_tree"
    try:
        for relative, identity in before.items():
            if relative == ".":
                continue
            original, copied = source / relative, staging / relative
            _safe_directory(original.parent)
            if _identity(original) != identity:
                raise ValueError("Folder changed while copying; source left untouched.")
            if identity[2] == -1:
                copied.mkdir()
            else:
                shutil.copyfile(original, copied, follow_symlinks=False)
                with copied.open("rb+") as stream:
                    os.fsync(stream.fileno())
                shutil.copystat(original, copied, follow_symlinks=False)
        if not _tree_matches(source, before):
            raise ValueError("Folder changed while copying; source left untouched.")
        # Preserve folder timestamps after all children have been copied.
        for relative in reversed(before):
            if before[relative][2] == -1:
                shutil.copystat(source / relative, staging / relative, follow_symlinks=False)
        entry[source_key] = before
        entry[tree_key] = _tree_snapshot(staging)
        entry[identity_key] = _identity(staging)
        atomic_write_json(history_file, history)
        _safe_directory(target.parent)
        _move_no_replace(staging, target)
        _remove_tree_verified(source, before)
    finally:
        if staging.exists():
            _safe_directory(staging)
            # This uniquely created staging directory is always directly under the chosen destination.
            if staging.parent == target.parent and staging.name.startswith(".desksweep-"):
                shutil.rmtree(staging)


# Reconcile interrupted moves from their recorded identities rather than guessing from filenames.
def _recover(history):
    for batch in history["batches"]:
        for move in batch["moves"]:
            source, target = _validate_move(move)
            target_signature = move.get("target_signature", move["signature"])
            restored_signature = move.get("restored_signature", move["signature"])
            if move["state"] == "pending":
                if _matches(target, target_signature):
                    move["state"] = "moved"
                elif _matches(source, move["signature"]):
                    move["state"] = "failed"
            elif move["state"] == "undo_pending":
                if _matches(source, restored_signature) and not target.exists():
                    move["state"] = "undone"
                elif _matches(target, target_signature):
                    move["state"] = "moved"


# Journal each intent before moving, then record success; individual failures leave other files safe.
def execute_moves(planned_moves, history_file="history.json"):
    history_file = Path(history_file).absolute()
    result = {"moved": [], "errors": [], "batch_id": None}
    if not planned_moves:
        return result
    with _history_lock(history_file):
        history = _read_history(history_file)
        _recover(history)
        batch = {"id": uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat(),
                 "moves": [], "created_dirs": []}
        history["batches"].append(batch)
        atomic_write_json(history_file, history)  # Fail before touching files if history is unwritable.
        result["batch_id"] = batch["id"]
        for plan in planned_moves:
            try:
                source, proposed = _validate_move(plan)
                if source in {history_file, Path(str(history_file) + ".lock")}:
                    raise ValueError("The history journal cannot be organized.")
                is_directory = plan.get("item_type", "file") == "directory"
                if is_directory and history_file.is_relative_to(source):
                    raise ValueError("A folder containing the history journal cannot be organized.")
                _safe_directory(source.parent)
                _safe_directory(proposed.parent.parent)
                signature = _signature(source)
                if source.is_dir() != is_directory:
                    raise ValueError("Item type changed after preview; scan again.")
                if plan.get("signature", signature) != signature:
                    raise ValueError("File changed after preview; scan again.")
                target = _unique_target(proposed.parent / source.name, is_directory=is_directory)
                if proposed.parent.exists():
                    _safe_directory(proposed.parent)
                    if _unsafe_entry(proposed.parent):
                        raise ValueError("The category folder is hidden or unsafe.")
                else:
                    proposed.parent.mkdir()
                    batch["created_dirs"].append(str(proposed.parent))
            except (OSError, ValueError, KeyError) as exc:
                result["errors"].append({"source_path": plan.get("source_path", "?"), "error": str(exc)})
                continue
            entry = {"filename": source.name, "source_path": str(source), "target_path": str(target),
                     "category": plan["category"], "signature": signature, "state": "pending",
                     "item_type": plan.get("item_type", "file"),
                     "destination_root": plan.get("destination_root", "")}
            batch["moves"].append(entry)
            atomic_write_json(history_file, history)
            try:
                _safe_directory(target.parent)
                if _signature(source) != signature:
                    raise ValueError("File changed just before moving; scan again.")
                _move_journaled(source, target, entry, history, history_file, "target_signature")
                entry["state"] = "moved"
                result["moved"].append(dict(entry))
            except (OSError, ValueError) as exc:
                # Keep pending if both names exist after an interrupted non-Windows hard-link move.
                entry["state"] = "pending" if _matches(target, entry.get("target_signature", signature)) else "failed"
                entry["error"] = str(exc)
                result["errors"].append({"source_path": str(source), "error": str(exc)})
            atomic_write_json(history_file, history)
        return result


# Restore the latest unfinished batch exactly; occupied paths and changed files remain retryable.
def undo_last_batch(history_file="history.json"):
    history_file = Path(history_file).absolute()
    result = {"restored": [], "errors": [], "batch_id": None}
    with _history_lock(history_file):
        history = _read_history(history_file)
        _recover(history)
        batch = next((b for b in reversed(history["batches"])
                      if any(m["state"] in {"moved", "pending", "undo_pending"} for m in b["moves"])
                      or (b["created_dirs"] and not b.get("undone_at"))), None)
        if batch is None:
            return result
        result["batch_id"] = batch["id"]
        for move in reversed(batch["moves"]):
            if move["state"] in {"undone", "failed"}:
                continue
            source, target = _validate_move(move)
            target_signature = move.get("target_signature", move["signature"])
            restored_signature = move.get("restored_signature", move["signature"])
            already_restored = False
            try:
                _safe_directory(source.parent)
                _safe_directory(target.parent)
                already_restored = (bool(move.get("target_signature") or move.get("restored_signature"))
                                    and _matches(source, restored_signature))
                if already_restored and move.get("item_type") == "directory":
                    already_restored = _tree_matches(source, move.get("restored_tree", move.get("source_tree")))
                if (source.exists() or source.is_symlink()) and not already_restored:
                    raise FileExistsError("Original path is occupied; move that file aside and retry undo.")
                if not _matches(target, target_signature):
                    raise ValueError("Organized file is missing, edited, or replaced; left untouched.")
            except (OSError, ValueError) as exc:
                result["errors"].append({"source_path": str(source), "error": str(exc)})
                continue
            move["state"] = "undo_pending"
            atomic_write_json(history_file, history)
            try:
                if already_restored:
                    if move.get("item_type") == "directory":
                        _remove_tree_verified(target, move.get("undo_source_tree") or move.get("target_tree"))
                    else:
                        target.unlink()  # An interrupted copy left two journal-verified copies.
                else:
                    _move_journaled(target, source, move, history, history_file, "restored_signature")
                move["state"] = "undone"
                result["restored"].append(str(source))
            except (OSError, ValueError) as exc:
                move["state"] = "undo_pending"
                result["errors"].append({"source_path": str(source), "error": str(exc)})
            atomic_write_json(history_file, history)
        for directory in reversed(batch["created_dirs"]):
            try:
                _safe_directory(Path(directory))
                Path(directory).rmdir()  # Only empty directories created by this batch are removed.
            except (OSError, ValueError):
                pass
        if all(m["state"] in {"undone", "failed"} for m in batch["moves"]):
            batch["undone_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(history_file, history)
    return result
