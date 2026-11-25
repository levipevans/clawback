"""Tray icon: a visible status light plus pause / resume / stop / quit.

Terminal-only app, but the tray gives the owner an always-visible control
surface. If the tray cannot load, the assistant still runs in the terminal.
"""

from __future__ import annotations

import os
import threading

from .controller import Snapshot, Status

_COLORS = {
    Status.STARTING: (150, 150, 150),
    Status.IDLE: (74, 144, 217),
    Status.RUNNING: (46, 204, 113),
    Status.PAUSED: (241, 196, 15),
    Status.ERROR: (231, 76, 60),
    Status.SANDBOX_UNAVAILABLE: (231, 76, 60),
    Status.STOPPED: (120, 120, 120),
}


def _image(color: tuple[int, int, int]):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=color, outline=(255, 255, 255, 255), width=3)
    draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 255))
    return image


class Tray:
    def __init__(self, runtime, *, on_quit):
        self.runtime = runtime
        self.on_quit = on_quit
        self._icon = None
        self._thread: threading.Thread | None = None
        self._failed = False

    def start(self) -> bool:
        try:
            import pystray  # noqa: F401
        except Exception as exc:
            self._failed = True
            print(f"tray unavailable ({type(exc).__name__}: {exc}); running terminal-only")
            return False
        self._thread = threading.Thread(target=self._run, name="clawback-tray", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        import pystray

        snapshot = self.runtime.controller.snapshot()
        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: f"Gibity Skibbit: {self.runtime.controller.snapshot().status.value}",
                lambda icon, item: None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Pause",
                self._pause,
                enabled=lambda item: not self.runtime.scheduler.paused,
            ),
            pystray.MenuItem(
                "Resume",
                self._resume,
                enabled=lambda item: self.runtime.scheduler.paused,
            ),
            pystray.MenuItem(
                "Stop current cycle",
                self._stop,
                enabled=lambda item: self.runtime.controller.snapshot().status is Status.RUNNING,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self._icon = pystray.Icon(
            "gibity-skibbit",
            _image(_COLORS.get(snapshot.status, (150, 150, 150))),
            f"Gibity Skibbit - {snapshot.status.value}",
            menu,
        )
        self.runtime.controller.subscribe(self._on_status)
        try:
            self._icon.run()
        except Exception:
            self._failed = True

    # pystray actions receive (icon, item)
    def _pause(self, *_args) -> None:
        self.runtime.scheduler.pause()

    def _resume(self, *_args) -> None:
        self.runtime.scheduler.resume()

    def _stop(self, *_args) -> None:
        self.runtime.scheduler.stop_current()

    def _quit(self, *_args) -> None:
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self) -> None:
        try:
            self.on_quit()
        finally:
            try:
                if self._icon is not None:
                    self._icon.stop()
            except Exception:
                pass
            # The console thread is blocked on input(); exit cleanly from here.
            os._exit(0)

    def _on_status(self, snapshot: Snapshot) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.icon = _image(_COLORS.get(snapshot.status, (150, 150, 150)))
            icon.title = f"Gibity Skibbit - {snapshot.status.value}"
            icon.update_menu()
        except Exception:
            pass


def stop_tray(tray: Tray | None) -> None:
    if tray is None or tray._icon is None:
        return
    try:
        tray._icon.stop()
    except Exception:
        pass
