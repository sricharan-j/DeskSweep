"""An in-memory tray icon and menu; this module never calls Tk."""

import threading

from PIL import Image, ImageDraw
import pystray


# Draw at four times the output size so the simple folder-and-broom silhouette stays crisp.
def create_tray_image(size=64):
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((16, 64, 132, 124), radius=14, fill="#4B88CC")
    draw.rounded_rectangle((16, 92, 229, 220), radius=18, fill="#7CB8FF")
    draw.line((190, 36, 118, 145), fill="#101318", width=28)
    draw.line((190, 36, 118, 145), fill="#F1F5F9", width=13)
    draw.polygon(((107, 125), (145, 151), (121, 213), (62, 174)), fill="#17283E")
    draw.polygon(((111, 136), (134, 152), (114, 198), (76, 173)), fill="#FFC76C")
    draw.line((100, 159, 87, 180), fill="#B97828", width=5)
    draw.line((116, 169, 104, 190), fill="#B97828", width=5)
    return image.resize((size, size), Image.Resampling.LANCZOS)


class TrayController:
    # Store a plain state snapshot so tray callbacks can read it without accessing any Tk variables.
    def __init__(self, notify, icon_factory=pystray.Icon):
        self._notify = notify
        self._lock = threading.Lock()
        self._watching = False
        self._enabled = False
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self.icon = icon_factory("DeskSweep", create_tray_image(), "DeskSweep",
                                menu=pystray.Menu(
                                    pystray.MenuItem("Open DeskSweep", self._open, default=True),
                                    pystray.MenuItem(self._watch_label, self._toggle,
                                                     checked=self._watch_checked, enabled=self._watch_enabled),
                                    pystray.Menu.SEPARATOR,
                                    pystray.MenuItem("Quit", self._exit)))
        self._thread = threading.Thread(target=self._run, name="DeskSweep-tray", daemon=False)

    # Start Windows' blocking tray message loop on its own thread, leaving Tk responsive.
    def start(self):
        self._thread.start()

    # Report startup failures to the UI so a window is never hidden without a working tray icon.
    def _run(self):
        try:
            self.icon.run(setup=self._setup)
        except Exception as exc:
            self._notify(("tray_error", str(exc)))
        finally:
            self._ready.set()

    # Publish the icon before acknowledging readiness, or honor an Exit requested during startup.
    def _setup(self, icon):
        try:
            if self._stop_requested.is_set():
                icon.stop()
                return
            icon.visible = True
            self._notify(("tray_ready",))
        except Exception as exc:
            self._notify(("tray_error", str(exc)))
            icon.stop()
        finally:
            self._ready.set()

    # Refresh the menu only when app state changes; pystray's message loop otherwise sleeps.
    def update_state(self, watching, enabled):
        with self._lock:
            self._watching, self._enabled = watching, enabled
        if self._ready.is_set() and not self._stop_requested.is_set():
            self.icon.update_menu()
            self.icon.title = f"DeskSweep — Auto-Watch {'On' if watching else 'Off'}"

    # Read the current label from thread-safe state rather than querying the GUI.
    def _watch_label(self, item):
        with self._lock:
            return f"Toggle Auto-Watch ({'On' if self._watching else 'Off'})"

    # Show a checkmark that stays synchronized with the actual watcher state.
    def _watch_checked(self, item):
        with self._lock:
            return self._watching

    # Disable toggling during filesystem work and shutdown to prevent overlapping operations.
    def _watch_enabled(self, item):
        with self._lock:
            return self._enabled

    # Request restoration through the event queue, never by changing widgets from this thread.
    def _open(self, icon, item):
        self._notify(("tray_open",))

    # Send a toggle request for the UI thread to validate and apply using the normal watcher workflow.
    def _toggle(self, icon, item):
        self._notify(("tray_toggle",))

    # Exit always requests real shutdown, even while Auto-Watch is running.
    def _exit(self, icon, item):
        self._notify(("tray_exit",))

    # Wait only for lifecycle events, stop the native loop, and join it before Tk is destroyed.
    def stop(self):
        self._stop_requested.set()
        if self._thread.ident is None:
            return
        if not self._ready.wait(timeout=10):
            raise RuntimeError("The tray did not finish starting; please retry Quit.")
        self.icon.stop()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("The tray is still stopping; please retry Quit.")
