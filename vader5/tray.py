"""System tray icon: controller connection, battery and gyro status, with a low-battery warning.

    ./vader5-tray

It reads the status file vader5-pad keeps up to date (see status.py), so it never talks to the
controller itself. Click the icon to open the settings; right-click for the menu.
"""

from __future__ import annotations

import glob
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from itertools import zip_longest

from PyQt6.QtCore import QTimer, qEnvironmentVariable
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from . import APP_NAME, config, launch
from . import status as status_file

LOW_BATTERY_PERCENT = 20
REFRESH_MS = 2000
OPEN_SETTINGS_GAP_S = 1.5  # clicks closer together than this don't start another launch
LOCK_WAIT_S = 3.0  # how long a new tray icon waits for an older one that's still closing
SERVICE = "vader5-pad.service"
STATUS_LINES = 4


WARNING_SOUNDS = ("battery-caution", "battery-low", "dialog-warning")  # sound theme names, best first
SOUND_PLAYERS = (["pw-play"], ["paplay"], ["canberra-gtk-play", "-f"])


def warning_sound_file(data_dirs: list[str] | None = None) -> str | None:
    """A low-battery sound from the desktop's sound themes (KDE's "battery-caution", for example)."""
    if data_dirs is None:
        data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    for name in WARNING_SOUNDS:
        for base in data_dirs:
            found = sorted(glob.glob(os.path.join(base, "sounds", "*", "stereo", name + ".og[ga]")))
            if found:
                return found[0]
    return None


def warning_sound_command(sound: str | None, which=shutil.which) -> list[str] | None:
    """How to play the sound with a player this system has, or None."""
    if sound is None:
        return None
    for player in SOUND_PLAYERS:
        if which(player[0]):
            return [*player, sound]
    return None


def low_battery_sound_wanted(path: str | None = None) -> bool:
    try:
        return config.load(path).low_battery_sound
    except config.ConfigError:
        return True  # the settings file has a mistake: use the default


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
        base = QIcon(launch.ICON_FILE) if os.path.exists(launch.ICON_FILE) else QIcon.fromTheme("input-gaming")
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
        self.icon.show()  # first: a message sent through an icon that isn't on the panel yet is lost
        self.refresh(warn=False)  # a battery warning waits for the next refresh, once the icon is up

    def refresh(self, warn: bool = True) -> None:
        current = status_file.read(self.status_path)
        self.view = describe(current)
        self.icon.setIcon(self.icon_active if self.view.active else self.icon_inactive)
        self.icon.setToolTip(self.view.tooltip)
        for action, text in zip_longest(self.line_actions, self.view.lines[:STATUS_LINES]):
            action.setVisible(text is not None)
            action.setText(text or "")
        self.service_action.setText("Stop background service" if self.view.service_running
                                    else "Start background service")
        if warn and self.warner.check(current):
            self.warn_low_battery(current["battery_percent"])

    def warn_low_battery(self, percent: int) -> None:
        self.icon.showMessage(f"Controller battery at {percent}%", "Charge the Vader 5 Pro soon.",
                              QSystemTrayIcon.MessageIcon.Warning, 10000)
        if low_battery_sound_wanted():
            command = warning_sound_command(warning_sound_file())
            if command:
                launch.start_detached(command)

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
        # KDE hands the tray a one-time token with each click; the window needs it to come to the front
        token = qEnvironmentVariable("XDG_ACTIVATION_TOKEN")
        if token:
            env["XDG_ACTIVATION_TOKEN"] = token
            os.unsetenv("XDG_ACTIVATION_TOKEN")
        launch.start_detached(launch.command("settings"), env=env)

    def toggle_service(self) -> None:
        command = "stop" if self.view.service_running else "start"
        subprocess.run(["systemctl", "--user", command, SERVICE], capture_output=True, timeout=15)
        QTimer.singleShot(1500, self.refresh)


def main(argv: list[str] | None = None) -> int:
    lock = status_file.single_instance_lock("vader5-tray")
    deadline = time.monotonic() + LOCK_WAIT_S
    while lock is None and time.monotonic() < deadline:  # e.g. setup just asked the old one to quit
        time.sleep(0.1)
        lock = status_file.single_instance_lock("vader5-tray")
    if lock is None:
        print("vader5-tray: already running", file=sys.stderr)
        return 1
    app = QApplication([sys.argv[0], *(sys.argv[1:] if argv is None else argv)])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("vader5-settings")  # the app's menu entry, so the desktop knows its name and icon
    app.setQuitOnLastWindowClosed(False)
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("vader5-tray: this desktop has no system tray", file=sys.stderr)
        return 1
    tray = Tray()  # noqa: F841 - kept alive for the life of the app
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
