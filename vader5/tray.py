"""System tray icon: controller connection, battery and gyro status, with a low-battery warning.

    ./vader5-tray

It reads the status file vader5-pad keeps up to date (see status.py), so it never talks to the
controller itself. Click the icon to open the settings; right-click for the menu.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from itertools import zip_longest

from PyQt6.QtCore import QTimer, qEnvironmentVariable
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from . import APP_NAME
from . import status as status_file

LOW_BATTERY_PERCENT = 20
REFRESH_MS = 2000
OPEN_SETTINGS_GAP_S = 1.5  # clicks closer together than this don't start another launch
SERVICE = "vader5-pad.service"
STATUS_LINES = 4


@dataclass
class TrayView:
    tooltip: str
    lines: list[str]  # status lines at the top of the menu
    active: bool  # the controller is on and sending input
    service_running: bool


def describe(status: dict | None) -> TrayView:
    """What the tray shows for a status (None = vader5-pad isn't running)."""
    if status is None:
        return TrayView(f"{APP_NAME}: background service isn't running",
                        ["Background service: not running"], False, False)
    if not status.get("connected"):
        return TrayView(f"{APP_NAME}: controller not connected",
                        ["Controller: not connected", "Background service: running"], False, True)
    connection = status.get("connection") or "connected"
    battery = status_file.battery_text(status)
    gyro = "on" if status.get("gyro") else "off"
    active = bool(status.get("active"))
    lines = [f"Controller: connected ({connection})" + ("" if active else ", idle")]
    if battery:
        lines.append(f"Battery: {battery}")
    lines.append(f"Gyro aiming: {gyro}")
    if status.get("profile"):
        lines.append(f"Profile: {status['profile']}")
    summary = [connection] + ([f"battery {battery}"] if battery else []) + [f"gyro {gyro}"]
    return TrayView(f"{APP_NAME}: " + " · ".join(summary), lines, active, True)


class LowBatteryWarner:
    """Warns once when the battery reaches the threshold, and again only after it has recovered."""

    def __init__(self, threshold: int = LOW_BATTERY_PERCENT):
        self.threshold = threshold
        self._warned = False

    def check(self, status: dict | None) -> bool:
        if not status or status.get("battery_percent") is None:
            return False  # unknown right now; keep what we knew
        low = status["battery_percent"] <= self.threshold and not status.get("charging")
        if not low:
            self._warned = False
            return False
        if self._warned:
            return False
        self._warned = True
        return True


class Tray:
    def __init__(self, status_path: str | None = None):
        self.status_path = status_path
        base = QIcon.fromTheme("input-gaming")
        if base.isNull():
            base = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.icon_active = base
        self.icon_inactive = QIcon(base.pixmap(64, 64, QIcon.Mode.Disabled))  # grey while off
        self.warner = LowBatteryWarner()
        self.view = describe(None)
        self._last_open = -OPEN_SETTINGS_GAP_S

        self.menu = QMenu()
        self.line_actions = [self.menu.addAction("") for _ in range(STATUS_LINES)]
        for action in self.line_actions:
            action.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction("Open settings…").triggered.connect(self.open_settings)
        self.service_action = self.menu.addAction("")
        self.service_action.triggered.connect(self.toggle_service)
        self.menu.addSeparator()
        self.menu.addAction("Quit tray icon").triggered.connect(QApplication.quit)

        self.icon = QSystemTrayIcon(self.icon_inactive)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()
        self.icon.show()

    def refresh(self) -> None:
        current = status_file.read(self.status_path)
        self.view = describe(current)
        self.icon.setIcon(self.icon_active if self.view.active else self.icon_inactive)
        self.icon.setToolTip(self.view.tooltip)
        for action, text in zip_longest(self.line_actions, self.view.lines[:STATUS_LINES]):
            action.setVisible(text is not None)
            action.setText(text or "")
        self.service_action.setText("Stop background service" if self.view.service_running
                                    else "Start background service")
        if self.warner.check(current):
            self.icon.showMessage("Vader 5 Pro battery low",
                                  f"The controller's battery is at {current['battery_percent']}%. Charge it soon.",
                                  QSystemTrayIcon.MessageIcon.Warning, 10000)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_settings()

    def open_settings(self) -> None:
        """Open the settings window, or bring it to the front if it's already open."""
        now = time.monotonic()
        if now - self._last_open < OPEN_SETTINGS_GAP_S:
            return
        self._last_open = now
        env = dict(os.environ)
        package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_parent, env.get("PYTHONPATH")]))
        # KDE hands the tray a one-time token with each click; the window needs it to come to the front
        token = qEnvironmentVariable("XDG_ACTIVATION_TOKEN")
        if token:
            env["XDG_ACTIVATION_TOKEN"] = token
            os.unsetenv("XDG_ACTIVATION_TOKEN")
        subprocess.Popen([sys.executable, "-m", "vader5.settings_window"], env=env, start_new_session=True)

    def toggle_service(self) -> None:
        command = "stop" if self.view.service_running else "start"
        subprocess.run(["systemctl", "--user", command, SERVICE], capture_output=True, timeout=15)
        QTimer.singleShot(1500, self.refresh)


def main(argv: list[str] | None = None) -> int:
    lock = status_file.single_instance_lock("vader5-tray")
    if lock is None:
        print("vader5-tray: already running", file=sys.stderr)
        return 1
    app = QApplication([sys.argv[0], *(sys.argv[1:] if argv is None else argv)])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("vader5-tray")
    app.setQuitOnLastWindowClosed(False)
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("vader5-tray: this desktop has no system tray", file=sys.stderr)
        return 1
    tray = Tray()  # noqa: F841 - kept alive for the life of the app
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
