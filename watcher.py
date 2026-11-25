"""Non-recursive, debounced watching; callbacks never access Tk widgets."""

from pathlib import Path
import os
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from organizer import destination_directory, execute_moves, scan_folders, validate_config, validate_settle_seconds, _safe_directory


if os.name == "nt":
    from watchdog.observers.api import BaseObserver
    from watchdog.observers.read_directory_changes import WindowsApiEmitter

    class QuietWindowsEmitter(WindowsApiEmitter):
        # Wake the idle buffer wait on shutdown before watchdog cancels its native directory read.
        def on_thread_stop(self):
            reader = getattr(self, "_reader", None)
            if reader is not None:
                with reader._buf_queue.not_empty:
                    reader._buf_queue.not_empty.notify_all()
            super().on_thread_stop()

        # Block for a native notification before draining watchdog 7's buffer without idle timeouts.
        def queue_events(self, timeout):
            reader = getattr(self, "_reader", None)
            if reader is not None:
                buffers = reader._buf_queue
                with buffers.not_empty:
                    buffers.not_empty.wait_for(lambda: bool(buffers.queue) or not self.should_keep_running())
                if self.should_keep_running():
                    super().queue_events(0)
            else:
                # watchdog 6 already blocks directly in ReadDirectoryChangesW.
                super().queue_events(timeout)


class _Events(FileSystemEventHandler):
    # Keep the event handler small so watchdog can continue receiving filesystem notifications.
    def __init__(self, owner):
        self.owner = owner

    # Queue new files without sorting old files that were present when watching began.
    def on_created(self, event):
        if not event.is_directory:
            self.owner.enqueue(event.src_path)

    # Use the destination name so a completed .crdownload rename becomes eligible.
    def on_moved(self, event):
        if not event.is_directory:
            self.owner.enqueue(event.dest_path)

    # Reset the settling timer only for new arrivals, leaving unrelated existing files alone.
    def on_modified(self, event):
        if not event.is_directory:
            self.owner.enqueue(event.src_path, existing_only=True)


class FolderWatcher:
    # Snapshot rules and paths so a running watcher has a consistent, predictable scope.
    def __init__(self, folder_paths, config, history_file="history.json", callback=None, settle_seconds=None):
        self.folders = list(dict.fromkeys(Path(p).expanduser().absolute() for p in folder_paths))
        self.config = validate_config(config)
        self.destination_root = destination_directory(self.config)
        self.history_file = Path(history_file).absolute()
        self.callback = callback or (lambda message: None)
        self.settle_seconds = validate_settle_seconds(
            self.config["settle_seconds"] if settle_seconds is None else settle_seconds)
        self._pending = {}
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._observer = BaseObserver(QuietWindowsEmitter) if os.name == "nt" else Observer()
        self._worker = threading.Thread(target=self._run, name="DeskSweep-sorter", daemon=False)
        self._started = False

    # Adjust pending quiet-period deadlines and wake the worker once when a user changes the delay.
    def set_settle_seconds(self, seconds):
        seconds = validate_settle_seconds(seconds)
        with self._changed:
            difference = seconds - self.settle_seconds
            self.settle_seconds = seconds
            for item in self._pending.values():
                item["due"] += difference
            self._changed.notify()

    # Validate every folder before starting so a partial setup cannot silently watch the wrong scope.
    def start(self):
        if self._started:
            raise RuntimeError("This watcher has already been started.")
        if not self.folders:
            raise ValueError("Select at least one folder before enabling Auto-Watch.")
        for folder in self.folders:
            _safe_directory(folder)
            if any(other != folder and other in folder.parents for other in self.folders):
                raise ValueError("Auto-Watch folders must not contain one another; choose one level at a time.")
        handler = _Events(self)
        try:
            for folder in self.folders:
                self._observer.schedule(handler, str(folder), recursive=False)
            self._observer.start()
            self._worker.start()
            self._started = True
        except Exception:
            self.stop()
            raise

    # Stop intake and join both threads so shutdown or undo cannot race a background move.
    def stop(self):
        self._stop.set()
        with self._changed:
            self._changed.notify_all()
        self._observer.stop()
        if self._observer.is_alive():
            self._observer.join()
        if self._worker.is_alive() and threading.current_thread() is not self._worker:
            self._worker.join()
        with self._lock:
            self._pending.clear()

    # Debounce events by file and snapshot size/time so active writes postpone organization.
    def enqueue(self, raw_path, existing_only=False):
        path = Path(raw_path).absolute()
        if (self._stop.is_set() or path.parent not in self.folders
                or (self.destination_root is not None and path.is_relative_to(self.destination_root))
                or path.name.startswith(".")
                or path.suffix.lower() in self.config["ignored_extensions"]
                or path in {self.history_file, Path(str(self.history_file) + ".lock")}):
            return
        try:
            info = path.stat()
        except OSError:
            return
        with self._lock:
            if existing_only and path not in self._pending:
                return
            previous = self._pending.get(path, {})
            self._pending[path] = {"due": time.monotonic() + self.settle_seconds,
                                   "signature": (info.st_size, info.st_mtime_ns),
                                   "attempts": previous.get("attempts", 0)}
            self._changed.notify()

    # Retry locked files a few times, then report them for a later manual preview.
    def _retry(self, path, item):
        item["attempts"] += 1
        if item["attempts"] >= 5:
            self.callback(f"Auto-Watch skipped {path.name} after 5 attempts. Use Scan & Preview to retry.")
            return
        with self._lock:
            item["due"] = time.monotonic() + self.settle_seconds
            self._pending.setdefault(path, item)
            self._changed.notify()

    # Sleep until an event, stop request, or a real settling deadline; never wake merely to poll.
    def _run(self):
        while True:
            with self._changed:
                while not self._stop.is_set():
                    if not self._pending:
                        self._changed.wait()
                    else:
                        delay = min(item["due"] for item in self._pending.values()) - time.monotonic()
                        if delay <= 0:
                            break
                        self._changed.wait(timeout=min(delay, threading.TIMEOUT_MAX))
                if self._stop.is_set():
                    return
                ready = [(p, item) for p, item in self._pending.items() if item["due"] <= time.monotonic()]
                for path, _ in ready:
                    del self._pending[path]
            for path, item in ready:
                if self._stop.is_set():
                    return
                try:
                    info = path.stat()
                    signature = (info.st_size, info.st_mtime_ns)
                    if signature != item["signature"]:
                        item["signature"] = signature
                        with self._lock:
                            item["due"] = time.monotonic() + self.settle_seconds
                            self._pending.setdefault(path, item)
                            self._changed.notify()
                        continue
                    plans = [p for p in scan_folders([path.parent], self.config)
                             if p["source_path"] == str(path)]
                    if self._stop.is_set() or not plans:
                        continue
                    result = execute_moves(plans, self.history_file)
                    if result["moved"]:
                        self.callback(f"Auto-Watch organized {path.name} → {result['moved'][0]['category']}")
                    elif result["errors"]:
                        self._retry(path, item)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as exc:
                    self.callback(f"Auto-Watch: {path.name}: {exc}")
                    self._retry(path, item)
