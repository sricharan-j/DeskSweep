"""Queued Tk notifications and event-driven CustomTkinter adaptations."""

import queue
import threading
import tkinter as tk

import customtkinter as ctk


class UiEvents:
    # Bind one virtual event so workers wake Tk only when there is actual work to process.
    def __init__(self, root, callback):
        self.root, self.callback = root, callback
        self.queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._wake_pending = False
        self._binding = root.bind("<<DeskSweepWork>>", self._deliver, add="+")

    # Queue data first, then ask Tk's thread-aware event system to deliver one coalesced wakeup.
    def put(self, event):
        with self._lock:
            if self._closed:
                return
            self.queue.put(event)
            if self._wake_pending:
                return
            self._wake_pending = True
        # Never hold our lock while Tk forwards this call to the main event loop.
        try:
            self.root.event_generate("<<DeskSweepWork>>", when="tail")
        except (RuntimeError, tk.TclError):
            with self._lock:
                self._wake_pending = False
                if not self._closed:
                    raise

    # Drain a bounded batch and schedule another idle callback only when queued work remains.
    def _deliver(self, event=None):
        with self._lock:
            if self._closed:
                return
            self._wake_pending = False
        self.callback()
        with self._lock:
            if not self._closed and not self.queue.empty() and not self._wake_pending:
                self._wake_pending = True
                self.root.after_idle(self._deliver)

    # Preserve Queue's nonblocking read interface for the application's main-thread event handler.
    def get_nowait(self):
        return self.queue.get_nowait()

    # Unbind only after workers and tray producers stop, preventing callbacks into a destroyed window.
    def close(self):
        with self._lock:
            self._closed = True
        self.root.unbind("<<DeskSweepWork>>", self._binding)


class EventDrivenWindow:
    # Suppress only CustomTkinter's recurring theme/DPI probes; keep all normal one-shot UI callbacks.
    def after(self, ms, func=None, *args):
        if func in (ctk.AppearanceModeTracker.update, ctk.ScalingTracker.check_dpi_scaling):
            return ""
        return super().after(ms, func, *args)

    # Recalculate monitor scaling on movement and mapping events instead of checking every 100 ms.
    def _update_dpi(self, event):
        if event.widget is not self or not self.winfo_viewable():
            return
        tracker = ctk.ScalingTracker
        scaling = tracker.get_window_dpi_scaling(self)
        if tracker.window_dpi_scaling_dict.get(self) != scaling:
            tracker.window_dpi_scaling_dict[self] = scaling
            tracker.update_scaling_callbacks_for_window(self)


class EventDrivenToplevel(EventDrivenWindow, ctk.CTkToplevel):
    # Give modal windows the same event-based DPI behavior as the main window.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bind("<Configure>", self._update_dpi, add="+")
        self.bind("<Map>", self._update_dpi, add="+")


class EventDrivenTextbox(ctk.CTkTextbox):
    # Update rule-editor scrollbars when its content or size changes, without a recurring timer.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._textbox.bind("<Configure>", self._check_if_scrollbars_needed, add="+")
        self._textbox.bind("<<Modified>>", self._content_changed, add="+")

    # Stop CustomTkinter's optional repeating scrollbar loop while retaining its layout calculation.
    def _check_if_scrollbars_needed(self, event=None, continue_loop=False):
        super()._check_if_scrollbars_needed(event, continue_loop=False)

    # Reset Tk's modification flag so each later edit can emit another change event.
    def _content_changed(self, event=None):
        if self._textbox.edit_modified():
            self._textbox.edit_modified(False)
            self._check_if_scrollbars_needed()
