"""Settings window for The Tux InVader, the Vader 5 Pro app.

    ./vader5-settings

Edits ~/.config/vader5/config.toml, including per-game profiles; vader5-pad applies saved changes
within about a second. It also shows whether the controller and the background service are running,
and can start or stop it.
"""

from __future__ import annotations

import dataclasses
import os
import socket as sockets
import subprocess
import sys
import time

from PyQt6.QtCore import QProcess, QSocketNotifier, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSlider, QTabWidget, QVBoxLayout, QWidget,
)

from . import APP_NAME, TAGLINE, config, install, launch, protocol
from . import stick_mouse
from . import led as lighting
from . import status as status_file
from .config import NONE, ConfigError, KeyCombo, Profile, Settings
from .device import find_config_hidraws
from .games import installed_steam_games
from .virtual_pad import BUTTON_CODES

SERVICE = "vader5-pad.service"
KEY = "__keyboard__"  # combo box choice that uses the text field

UI_ORDER = (
    "A", "B", "X", "Y", "LB", "RB", "LT", "RT", "L3", "R3", "SELECT", "START", "HOME",
    "UP", "DOWN", "LEFT", "RIGHT", "M1", "M2", "M3", "M4", "C", "Z", "LM", "RM", "FN", "TURBO",
)
BUTTON_LABELS = {
    "A": "A", "B": "B", "X": "X", "Y": "Y", "LB": "LB (left bumper)", "RB": "RB (right bumper)",
    "LT": "LT full press", "RT": "RT full press", "L3": "L3 (left stick click)", "R3": "R3 (right stick click)",
    "SELECT": "View / Select", "START": "Menu / Start", "HOME": "Home",
    "UP": "D-pad up", "DOWN": "D-pad down", "LEFT": "D-pad left", "RIGHT": "D-pad right",
    "M1": "M1 (back)", "M2": "M2 (back)", "M3": "M3 (back)", "M4": "M4 (back)",
    "C": "C", "Z": "Z", "LM": "LM", "RM": "RM", "FN": "FN", "TURBO": "Turbo",
}
MOUSE_BUTTONS = ("left", "right", "middle", "back", "forward")
ERROR_STYLE = "color: #e5534b;"
OK_STYLE = "color: #57ab5a;"
HINT_STYLE = "color: gray;"
WARNING_STYLE = "color: #c69026;"
NOT_CONNECTED = "Not connected: turn the controller on, or plug in the cable or dongle"


def firmware_text(firmware: str | None, connected: bool) -> tuple[str, bool]:
    """The firmware line at the top of the window, and whether it's a warning."""
    if not firmware:
        return "Firmware: shown once the controller is connected and the background service is running", False
    text = f"Firmware: {firmware}" + ("" if connected else " (last connected controller)")
    note = protocol.firmware_note(firmware)
    return (f"{text} · {note}", True) if note else (f"{text} · tested with this app", False)


def controller_summary(current: dict | None, plugged_in: bool) -> str:
    """One line about the controller: from vader5-pad's status while it runs, else from what's plugged in."""
    if current is None:
        return "Connected" if plugged_in else NOT_CONNECTED
    if not current.get("connected"):
        return NOT_CONNECTED
    parts = [f"Connected ({current.get('connection') or 'unknown connection'})"]
    battery = status_file.battery_text(current)
    if battery:
        parts.append(f"battery {battery}")
    parts.append(f"gyro aiming {'on' if current.get('gyro') else 'off'}")
    if current.get("profile"):
        parts.append(f"profile {current['profile']}")
    return " · ".join(parts)


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(HINT_STYLE)
    return label


class SliderSpin(QWidget):
    """A slider and a number box that stay in sync. The number box allows the full range."""

    def __init__(self, low: float, high: float, step: float, decimals: int, slider_high: float | None = None):
        super().__init__()
        self._step = step
        self.spin = QDoubleSpinBox()
        self.spin.setRange(low, high)
        self.spin.setSingleStep(step)
        self.spin.setDecimals(decimals)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(round(low / step), round((slider_high if slider_high is not None else high) / step))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)
        self.slider.valueChanged.connect(lambda position: self.spin.setValue(position * step))
        self.spin.valueChanged.connect(self._move_slider)
        self.valueChanged = self.spin.valueChanged

    def _move_slider(self, value: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(round(value / self._step))
        self.slider.blockSignals(False)

    def value(self) -> float:
        return round(self.spin.value(), self.spin.decimals())

    def setValue(self, value: float) -> None:
        self.spin.setValue(value)


def color_icon(color: tuple[int, int, int]) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(QColor(*color))
    return QIcon(pixmap)


class ButtonColors(QWidget):
    """Pick a controller button in the list, then click the color box to give it a flash color."""

    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._colors: dict[str, tuple[int, int, int]] = {}
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.button = QComboBox()
        for name in UI_ORDER:
            self.button.addItem(BUTTON_LABELS[name], name)
        self.swatch = QPushButton()
        self.swatch.setFixedSize(40, 26)
        self.clear_button = QPushButton("No flash")
        self.clear_button.setToolTip("This button doesn't light the strip")
        row.addWidget(self.button, 1)
        row.addWidget(self.swatch)
        row.addWidget(self.clear_button)
        column.addLayout(row)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(self.summary)
        self.button.currentIndexChanged.connect(lambda _: self._refresh())
        self.swatch.clicked.connect(self._pick)
        self.clear_button.clicked.connect(self._clear)
        self._refresh()

    def button_colors(self) -> tuple[tuple[str, tuple[int, int, int]], ...]:
        return tuple((name, self._colors[name]) for name in UI_ORDER if name in self._colors)

    def set_button_colors(self, pairs) -> None:
        self._colors = dict(pairs)
        self._refresh()

    def set_color(self, name: str, color: tuple[int, int, int] | None) -> None:
        """Give a button its flash color, or None for no flash."""
        if color is None:
            self._colors.pop(name, None)
        else:
            self._colors[name] = color
        self._refresh()
        self.changed.emit()

    def _pick(self) -> None:
        name = self.button.currentData()
        chosen = QColorDialog.getColor(QColor(*self._colors.get(name, lighting.DEFAULT_COLOR)), self,
                                       f"Flash color for {BUTTON_LABELS[name]}")
        if chosen.isValid():
            self.set_color(name, (chosen.red(), chosen.green(), chosen.blue()))

    def _clear(self) -> None:
        self.set_color(self.button.currentData(), None)

    def _refresh(self) -> None:
        name = self.button.currentData()
        color = self._colors.get(name)
        if color:
            self.swatch.setText("")
            self.swatch.setStyleSheet(f"background-color: {lighting.color_text(color)}; border: 1px solid gray;")
            self.swatch.setToolTip(f"{lighting.color_text(color)} (click to change)")
        else:
            self.swatch.setText("+")
            self.swatch.setStyleSheet("border: 1px dashed gray;")
            self.swatch.setToolTip("Click to choose a flash color for this button")
        self.clear_button.setEnabled(color is not None)
        for index in range(self.button.count()):
            chosen = self._colors.get(self.button.itemData(index))
            self.button.setItemIcon(index, color_icon(chosen) if chosen else QIcon())
        if not self._colors:
            self.summary.setText("No buttons chosen yet: pick a button, then click the color box next to it.")
            return
        chips = []
        for button, chosen in self.button_colors():
            text = "#000000" if sum(chosen) > 380 else "#ffffff"
            chips.append(f'<span style="background-color: {lighting.color_text(chosen)}; color: {text};">'
                         f"&nbsp;{button}&nbsp;</span>")
        self.summary.setText("Flashing: " + " ".join(chips))


class ColorList(QWidget):
    """A row of color swatches: click one to change it; + adds a color, − removes the last one."""

    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._colors: list[tuple[int, int, int]] = [lighting.DEFAULT_COLOR]
        self._minimum, self._maximum = 1, lighting.MAX_COLORS
        self._swatches: list[QPushButton] = []
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self.add_button = QPushButton("+")
        self.remove_button = QPushButton("−")
        for button in (self.add_button, self.remove_button):
            button.setFixedWidth(32)
        self.add_button.setToolTip("Add a color")
        self.remove_button.setToolTip("Remove the last color")
        self.add_button.clicked.connect(self._add)
        self.remove_button.clicked.connect(self._remove)
        self._row.addWidget(self.add_button)
        self._row.addWidget(self.remove_button)
        self._row.addStretch(1)
        self._rebuild()

    def colors(self) -> tuple[tuple[int, int, int], ...]:
        return tuple(self._colors)

    def set_colors(self, colors) -> None:
        self._colors = list(colors) or [lighting.DEFAULT_COLOR]
        self._rebuild()
        self.changed.emit()

    def set_limits(self, minimum: int, maximum: int, trim: bool) -> None:
        """How many colors the effect uses. `trim` drops or adds colors to fit (when the user picks an effect)."""
        self._minimum, self._maximum = minimum, maximum
        if trim and maximum:
            self._colors = self._colors[:maximum]
            while len(self._colors) < minimum:
                self._colors.append(self._colors[-1] if self._colors else lighting.DEFAULT_COLOR)
        self._rebuild()

    def _rebuild(self) -> None:
        for swatch in self._swatches:
            self._row.removeWidget(swatch)
            swatch.deleteLater()
        self._swatches = []
        for index, color in enumerate(self._colors):
            swatch = QPushButton()
            swatch.setFixedSize(40, 26)
            swatch.setToolTip(f"{lighting.color_text(color)} (click to change)")
            swatch.setStyleSheet(f"background-color: {lighting.color_text(color)}; border: 1px solid gray;")
            swatch.clicked.connect(lambda _checked=False, i=index: self._pick(i))
            self._row.insertWidget(index, swatch)
            self._swatches.append(swatch)
        several = self._maximum > 1
        self.add_button.setVisible(several)
        self.remove_button.setVisible(several)
        self.add_button.setEnabled(len(self._colors) < self._maximum)
        self.remove_button.setEnabled(len(self._colors) > max(1, self._minimum))

    def _pick(self, index: int) -> None:
        chosen = QColorDialog.getColor(QColor(*self._colors[index]), self, "Choose a color")
        if chosen.isValid():
            self._colors[index] = (chosen.red(), chosen.green(), chosen.blue())
            self._rebuild()
            self.changed.emit()

    def _add(self) -> None:
        if len(self._colors) < self._maximum:
            self._colors.append(self._colors[-1])
            self._rebuild()
            self.changed.emit()

    def _remove(self) -> None:
        if len(self._colors) > max(1, self._minimum):
            self._colors.pop()
            self._rebuild()
            self.changed.emit()


class RemapRow:
    """One button on the Buttons tab: what it sends instead."""

    def __init__(self, name: str, grid: QGridLayout, row: int, on_change):
        self.name = name
        self.combo = QComboBox()
        self.combo.addItem("Itself (no change)", "")
        self.combo.addItem("Nothing (turned off)", NONE)
        for target in UI_ORDER:
            if target in BUTTON_CODES:
                self.combo.addItem(f"Controller: {BUTTON_LABELS[target]}", target)
        for mouse in MOUSE_BUTTONS:
            self.combo.addItem(f"Mouse: {mouse} button", f"mouse:{mouse}")
        self.combo.addItem("Keyboard key…", KEY)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("e.g. space, f13 or ctrl+c")
        self.note = hint("")
        grid.addWidget(QLabel(BUTTON_LABELS[name]), row, 0)
        grid.addWidget(self.combo, row, 1)
        grid.addWidget(self.edit, row, 2)
        grid.addWidget(self.note, row, 3)
        self.combo.currentIndexChanged.connect(lambda _: (self._sync(), on_change()))
        self.edit.textChanged.connect(lambda _: on_change())
        self._role: str | None = None
        self._sync()

    def _sync(self) -> None:
        wants_key = self.combo.currentData() == KEY
        self.edit.setVisible(wants_key)  # only shown for "Keyboard key…"
        self.edit.setEnabled(self._role is None and wants_key)

    def set_target(self, target: str | None) -> None:
        """Show a remap target as written in the settings file (None = no remap)."""
        self.edit.clear()
        if not target:
            index = 0
        else:
            index = self.combo.findData(target.lower() if target.lower().startswith("mouse:") else target)
            if index < 0:
                index = self.combo.findData(KEY)
                is_plain_keys = target.lower().startswith("key:") and "mouse:" not in target.lower()
                self.edit.setText(target[4:] if is_plain_keys else target)
        self.combo.setCurrentIndex(index)
        self._sync()

    def target(self) -> str | None:
        """The remap target to write, or None to leave the button as itself."""
        choice = self.combo.currentData()
        if choice == "":
            return None
        if choice == KEY:
            text = self.edit.text().strip()
            if not text:
                raise ConfigError(f"{BUTTON_LABELS[self.name]}: type a key, like space or ctrl+c")
            return text if ":" in text else f"key:{text}"
        return choice

    def set_role(self, role: str | None) -> None:
        """Buttons used by the gyro can't be remapped; say so instead."""
        self._role = role
        self.combo.setEnabled(role is None)
        self.note.setText(role or "")
        self._sync()


class AddProfileDialog(QDialog):
    """Pick the game for a new profile: an installed Steam game, or a program name."""

    def __init__(self, parent: QWidget, games: list[tuple[int, str]]):
        super().__init__(parent)
        self.setWindowTitle("Add game profile")
        form = QFormLayout(self)
        self.game = QComboBox()
        for app_id, name in games:
            self.game.addItem(name, app_id)
        self.game.addItem("Another program (not on Steam)…", None)
        self.program = QLineEdit()
        self.program.setPlaceholderText("program file name, e.g. game.exe")
        self.name = QLineEdit()
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(ERROR_STYLE)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        form.addRow("Game:", self.game)
        form.addRow("Program:", self.program)
        form.addRow("Profile name:", self.name)
        form.addRow(hint("Steam games are recognized automatically while they run. For other games, type the "
                         "program's file name as shown in System Monitor."))
        form.addRow(self.error)
        form.addRow(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.game.currentIndexChanged.connect(lambda _: self._game_changed())
        self._game_changed()

    def _game_changed(self) -> None:
        is_program = self.game.currentData() is None
        self.program.setEnabled(is_program)
        if not is_program:
            self.name.setText(self.game.currentText())

    def steam_app_id(self) -> int | None:
        return self.game.currentData()

    def process(self) -> str | None:
        return self.program.text() if self.game.currentData() is None else None


class SettingsWindow(QMainWindow):
    def __init__(self, path: str | None = None, manage_service: bool = True):
        super().__init__()
        self.path = path or config.config_path()
        self.manage_service = manage_service
        self._saved_text: str | None = None
        self._loading = False
        self._switching = False
        self.main_settings = Settings()  # the main settings, without profiles
        self.profiles: list[Profile] = []  # each profile's own settings (overrides)
        self.current = -1  # -1 = editing the main settings, else the index of the profile being edited
        self._steam_games: dict[int, str] | None = None
        self._last_firmware: str | None = None
        self.setWindowTitle(APP_NAME)
        self.resize(780, 880)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_header())
        layout.addWidget(self._build_status())
        layout.addLayout(self._build_profile_bar())
        self.profile_hint = hint("")
        layout.addWidget(self.profile_hint)
        tabs = QTabWidget()
        tabs.addTab(self._build_gyro_tab(), "Gyro")
        tabs.addTab(self._build_sticks_tab(), "Sticks")
        tabs.addTab(self._build_buttons_tab(), "Buttons")
        tabs.addTab(self._build_led_tab(), "LED")
        layout.addWidget(tabs, 1)

        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.defaults_button = QPushButton("Restore defaults")
        self.revert_button = QPushButton("Revert")
        self.save_button = QPushButton("Save")
        self.save_button.setDefault(True)
        buttons.addWidget(self.defaults_button)
        buttons.addStretch(1)
        buttons.addWidget(self.revert_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.setCentralWidget(central)

        self.defaults_button.clicked.connect(self.restore_defaults)
        self.revert_button.clicked.connect(self.revert)
        self.save_button.clicked.connect(self.save)

        self.revert()
        if manage_service:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.refresh_status)
            self._timer.start(2000)
        self.refresh_status()

    # ------------------------------------------------------------------ building the window

    def _build_header(self) -> QWidget:
        header = QWidget()
        column = QVBoxLayout(header)
        column.setContentsMargins(0, 0, 0, 4)
        name = QLabel(APP_NAME)
        font = name.font()
        font.setPointSizeF(font.pointSizeF() * 1.6)
        font.setBold(True)
        name.setFont(font)
        self.firmware_label = QLabel()
        self.firmware_label.setWordWrap(True)
        column.addWidget(name)
        column.addWidget(self.firmware_label)
        column.addWidget(hint(TAGLINE))
        return header

    def _build_status(self) -> QGroupBox:
        box = QGroupBox("Status")
        grid = QGridLayout(box)
        self.controller_status = QLabel()
        self.controller_status.setWordWrap(True)
        self.service_status = QLabel()
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.setup_button = QPushButton("Set up…")
        self.setup_button.setToolTip("Installs the permissions rule (asks for your password once), the background "
                                     "service and the app menu entry")
        self.autostart = QCheckBox("Start automatically when I log in")
        grid.addWidget(QLabel("Controller:"), 0, 0)
        grid.addWidget(self.controller_status, 0, 1, 1, 3)
        grid.addWidget(QLabel("Background service:"), 1, 0)
        grid.addWidget(self.service_status, 1, 1)
        grid.addWidget(self.start_button, 1, 2)
        grid.addWidget(self.stop_button, 1, 3)
        grid.addWidget(self.autostart, 2, 1, 1, 2)
        grid.addWidget(self.setup_button, 2, 3)
        self.battery_sound = QCheckBox("Play a sound with the low-battery warning (20%)")
        self.battery_sound.setToolTip("The tray icon shows a warning once when the controller's battery is at 20% "
                                      "or lower; this adds the desktop's battery sound")
        grid.addWidget(self.battery_sound, 3, 1, 1, 3)
        self.battery_sound.toggled.connect(lambda _: self._changed())
        grid.setColumnStretch(1, 1)
        self.start_button.clicked.connect(lambda: self._service("start"))
        self.stop_button.clicked.connect(lambda: self._service("stop"))
        self.setup_button.clicked.connect(self.run_setup)
        self.autostart.clicked.connect(lambda checked: self._service("enable" if checked else "disable"))
        return box

    def _build_profile_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self.profile_select = QComboBox()
        self.add_profile_button = QPushButton("Add game profile…")
        self.delete_profile_button = QPushButton("Delete profile")
        bar.addWidget(QLabel("Editing:"))
        bar.addWidget(self.profile_select, 1)
        bar.addWidget(self.add_profile_button)
        bar.addWidget(self.delete_profile_button)
        self.profile_select.currentIndexChanged.connect(self._profile_selected)
        self.add_profile_button.clicked.connect(self._ask_for_profile)
        self.delete_profile_button.clicked.connect(lambda: self.delete_profile())
        return bar

    def _build_gyro_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.addRow(hint("Use the gyro button on the controller to start aiming: a short light buzz means on, a "
                         "longer heavy buzz means off. Turning the controller then moves your aim."))
        self.gyro_button = QComboBox()
        for name in UI_ORDER:
            self.gyro_button.addItem(BUTTON_LABELS[name], name)
        self.gyro_ratchet = QComboBox()
        self.gyro_ratchet.addItem("None", NONE)
        for name in UI_ORDER:
            if name != "TURBO":
                self.gyro_ratchet.addItem(BUTTON_LABELS[name], name)
        self.gyro_mode = QComboBox()
        self.gyro_mode.addItem("Press to switch aiming on and off", "toggle")
        self.gyro_mode.addItem("Hold it to aim", "hold")
        self.gyro_space = QComboBox()
        self.gyro_space.addItem("The controller's own axis", "controller")
        self.gyro_space.addItem("The room's up direction (player space)", "player")
        self.sensitivity = SliderSpin(0, 1000, 0.5, 1, slider_high=50)
        self.horizontal_scale = SliderSpin(0, 10, 0.05, 2, slider_high=3)
        self.vertical_scale = SliderSpin(0, 10, 0.05, 2, slider_high=3)
        self.invert_x = QCheckBox("Invert left / right")
        self.invert_y = QCheckBox("Invert up / down")
        self.tightening = SliderSpin(0, 20, 0.1, 1, slider_high=5)

        form.addRow("Gyro button:", self.gyro_button)
        form.addRow("How to use it:", self.gyro_mode)
        form.addRow("Pause while held (ratchet):", self.gyro_ratchet)
        form.addRow("", hint("Hold it to bring your hands back to center without moving your aim."))
        form.addRow("Turn left / right around:", self.gyro_space)
        form.addRow("", hint("Player space uses gravity, so turning feels the same however tilted you hold "
                             "the controller."))
        form.addRow("Sensitivity:", self.sensitivity)
        form.addRow("", hint("Mouse movement per degree you turn the controller."))
        form.addRow("Left / right speed:", self.horizontal_scale)
        form.addRow("Up / down speed:", self.vertical_scale)
        form.addRow("", hint("Extra speed for one direction: 1.25 is a quarter faster, 0.8 a fifth slower."))
        form.addRow("", self.invert_x)
        form.addRow("", self.invert_y)
        form.addRow("Steadiness (tightening):", self.tightening)
        form.addRow("", hint("Turning slower than this many degrees per second is softened. "
                             "Lower feels more responsive, higher feels steadier. 0 turns it off."))
        for widget in (self.gyro_button, self.gyro_mode, self.gyro_ratchet, self.gyro_space):
            widget.currentIndexChanged.connect(lambda _: self._changed())
        for widget in (self.sensitivity, self.horizontal_scale, self.vertical_scale, self.tightening):
            widget.valueChanged.connect(lambda _: self._changed())
        for widget in (self.invert_x, self.invert_y):
            widget.toggled.connect(lambda _: self._changed())
        return page

    def _build_sticks_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.addRow(hint("A deadzone ignores small movements around the center of a stick, for a stick that "
                         "drifts. Games have their own deadzones, so leave these at 0 unless you need them."))
        self.left_deadzone = SliderSpin(0, 0.9, 0.01, 2)
        self.right_deadzone = SliderSpin(0, 0.9, 0.01, 2)
        form.addRow("Left stick deadzone:", self.left_deadzone)
        form.addRow("Right stick deadzone:", self.right_deadzone)
        form.addRow("", hint("0.10 ignores the first 10% of the stick's travel."))

        form.addRow(hint("\nHold a button to move the mouse pointer with a stick, for menus and maps. While you "
                         "hold it, that stick doesn't reach the game, so nothing moves on screen. Put a mouse "
                         "click on another button in the Buttons tab to click with it."))
        self.pointer_button = QComboBox()
        self.pointer_button.addItem("Off", NONE)
        for name in UI_ORDER:
            if name != "TURBO":  # Turbo only sends a short pulse, so it can't be held
                self.pointer_button.addItem(BUTTON_LABELS[name], name)
        self.pointer_stick = QComboBox()
        for name, label in stick_mouse.STICKS.items():
            self.pointer_stick.addItem(label, name)
        self.pointer_speed = SliderSpin(50, 5000, 25, 0, slider_high=2500)
        self.pointer_deadzone = SliderSpin(0, 0.9, 0.01, 2)
        self.pointer_curve = SliderSpin(1, 3, 0.1, 1)
        form.addRow("Hold to point:", self.pointer_button)
        form.addRow("Point with:", self.pointer_stick)
        form.addRow("Pointer speed:", self.pointer_speed)
        form.addRow("", hint("Pixels a second at full tilt."))
        form.addRow("Pointer deadzone:", self.pointer_deadzone)
        form.addRow("Fine control:", self.pointer_curve)
        form.addRow("", hint("1.0 moves straight with the stick; higher gives finer control near the center."))
        self._pointer_rows = (self.pointer_stick, self.pointer_speed, self.pointer_deadzone, self.pointer_curve)
        self._sticks_form = form
        self.pointer_button.currentIndexChanged.connect(lambda _: (self._sync_pointer_controls(), self._changed()))
        self.pointer_stick.currentIndexChanged.connect(lambda _: self._changed())
        for widget in (self.left_deadzone, self.right_deadzone, self.pointer_speed, self.pointer_deadzone,
                       self.pointer_curve):
            widget.valueChanged.connect(lambda _: self._changed())
        self._sync_pointer_controls()
        return page

    def _sync_pointer_controls(self) -> None:
        """The pointer settings only matter once a button is chosen to hold."""
        on = self.pointer_button.currentData() != NONE
        for widget in self._pointer_rows:
            self._sticks_form.setRowVisible(widget, on)

    def _build_buttons_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(hint("Choose what each button sends instead: another controller button, a mouse button, "
                              "a keyboard key or combination (like ctrl+c), or nothing."))
        inner = QWidget()
        grid = QGridLayout(inner)
        self.remap_rows: dict[str, RemapRow] = {}
        for row, name in enumerate(UI_ORDER):
            self.remap_rows[name] = RemapRow(name, grid, row, self._changed)
        grid.setColumnStretch(2, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)
        return page

    def _build_led_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.addRow(hint("Choose what the LED strip on the controller shows. The background service sends it within "
                         "a second of saving. Nothing is stored on the controller itself, so switching it off and on "
                         "shows its own lights until the service sends yours again."))
        self.led_effect = QComboBox()
        for effect in lighting.EFFECTS.values():
            self.led_effect.addItem(effect.label + (" (experimental)" if effect.experimental else ""), effect.name)
        self.led_effect_hint = hint("")
        self.led_colors = ColorList()
        self.led_brightness = SliderSpin(0, 100, 1, 0)
        self.led_speed = SliderSpin(1, 10, 1, 0)
        form.addRow("Effect:", self.led_effect)
        form.addRow("", self.led_effect_hint)
        form.addRow("Colors:", self.led_colors)
        form.addRow("Brightness:", self.led_brightness)
        form.addRow("Speed:", self.led_speed)
        form.addRow("", hint("Speed goes from 1 (slow) to 10 (fast)."))
        self.led_specific = QCheckBox("Specific buttons")
        self.led_button_colors = ButtonColors()
        self.led_specific_hint = hint("Only the buttons you give a color flash, each in its own color. The colors "
                                      "above aren't used while this is on.")
        form.addRow("", self.led_specific)
        form.addRow("Buttons:", self.led_button_colors)
        form.addRow("", self.led_specific_hint)
        self._led_form = form
        self.led_effect.currentIndexChanged.connect(lambda _: (self._sync_led_controls(trim=True), self._changed()))
        self.led_specific.toggled.connect(lambda _: (self._sync_led_controls(trim=False), self._changed()))
        self.led_button_colors.changed.connect(self._changed)
        self.led_colors.changed.connect(self._changed)
        for widget in (self.led_brightness, self.led_speed):
            widget.valueChanged.connect(lambda _: self._changed())
        self._sync_led_controls(trim=False)
        return page

    def _sync_led_controls(self, trim: bool) -> None:
        effect = lighting.EFFECTS[self.led_effect.currentData()]
        text = effect.hint
        if effect.experimental:
            text += " Experimental: built from animation frames, so it may look different on the controller."
        self.led_effect_hint.setText(text)
        if effect.max_colors:
            self.led_colors.set_limits(effect.min_colors, effect.max_colors, trim)
        flash = effect.name == "press_flash"
        specific = flash and self.led_specific.isChecked()
        self.led_colors.setEnabled(effect.max_colors > 0 and not specific)
        self._led_form.setRowVisible(self.led_specific, flash)
        self._led_form.setRowVisible(self.led_button_colors, specific)
        self._led_form.setRowVisible(self.led_specific_hint, specific)
        self.led_brightness.setEnabled(effect.name not in ("controller", "off"))
        self.led_speed.setEnabled(effect.animated)

    # ------------------------------------------------------------------ settings <-> window

    def _show_settings(self, settings: Settings) -> None:
        self._loading = True
        gyro = settings.gyro
        self.gyro_button.setCurrentIndex(self.gyro_button.findData(gyro.button))
        self.gyro_mode.setCurrentIndex(max(0, self.gyro_mode.findData(gyro.mode)))
        self.gyro_ratchet.setCurrentIndex(max(0, self.gyro_ratchet.findData(gyro.ratchet)))
        self.gyro_space.setCurrentIndex(max(0, self.gyro_space.findData(gyro.space)))
        self.sensitivity.setValue(gyro.sensitivity)
        self.horizontal_scale.setValue(gyro.horizontal_scale)
        self.vertical_scale.setValue(gyro.vertical_scale)
        self.invert_x.setChecked(gyro.invert_x)
        self.invert_y.setChecked(gyro.invert_y)
        self.tightening.setValue(gyro.tightening_dps)
        self.left_deadzone.setValue(settings.left_deadzone)
        self.right_deadzone.setValue(settings.right_deadzone)
        pointer = settings.pointer
        self.pointer_button.setCurrentIndex(max(0, self.pointer_button.findData(pointer.button)))
        self.pointer_stick.setCurrentIndex(max(0, self.pointer_stick.findData(pointer.stick)))
        self.pointer_speed.setValue(pointer.speed)
        self.pointer_deadzone.setValue(pointer.deadzone)
        self.pointer_curve.setValue(pointer.curve)
        self._sync_pointer_controls()
        for name, row in self.remap_rows.items():
            row.set_target(settings.target(name))
        lights = settings.led
        self.led_effect.setCurrentIndex(max(0, self.led_effect.findData(lights.effect)))
        self.led_colors.set_colors(lights.colors)
        self.led_brightness.setValue(lights.brightness)
        self.led_speed.setValue(lights.speed)
        self.led_specific.setChecked(lights.specific_buttons)
        self.battery_sound.setChecked(settings.low_battery_sound)
        self.led_button_colors.set_button_colors(lights.button_colors)
        self._sync_led_controls(trim=False)
        self._loading = False
        self._changed()

    def _form_settings(self) -> Settings:
        """The settings shown on the tabs (without profiles), checked like the settings file. Raises ConfigError."""
        settings = Settings()
        gyro = settings.gyro
        gyro.button = self.gyro_button.currentData()
        gyro.mode = self.gyro_mode.currentData()
        gyro.ratchet = self.gyro_ratchet.currentData()
        gyro.space = self.gyro_space.currentData()
        gyro.sensitivity = self.sensitivity.value()
        gyro.horizontal_scale = self.horizontal_scale.value()
        gyro.vertical_scale = self.vertical_scale.value()
        gyro.invert_x = self.invert_x.isChecked()
        gyro.invert_y = self.invert_y.isChecked()
        gyro.tightening_dps = self.tightening.value()
        settings.left_deadzone = self.left_deadzone.value()
        settings.right_deadzone = self.right_deadzone.value()
        settings.pointer = stick_mouse.StickMouseSettings(
            self.pointer_button.currentData(), self.pointer_stick.currentData(),
            self.pointer_speed.value(), self.pointer_deadzone.value(), self.pointer_curve.value(),
        )
        for name, row in self.remap_rows.items():
            target = row.target()
            if target is None:
                continue
            if ":" in target:
                settings.key_remap[name] = KeyCombo(target, ())
            else:
                settings.remap[name] = target
        settings.led = lighting.LedSettings(
            self.led_effect.currentData(), self.led_colors.colors(),
            int(round(self.led_brightness.value())), int(round(self.led_speed.value())),
            self.led_specific.isChecked(), self.led_button_colors.button_colors(),
        )
        settings.low_battery_sound = self.battery_sound.isChecked()
        return config.parse(config.render(settings))

    def settings_from_window(self) -> Settings:
        """Everything in the window (main settings and profiles), checked like the settings file. Raises ConfigError."""
        form = self._form_settings()
        main, profiles = self.main_settings, list(self.profiles)
        if self.current < 0:
            main = form
        else:
            profiles[self.current] = dataclasses.replace(
                profiles[self.current], overrides=config.overrides_between(main, form))
        main = dataclasses.replace(main, low_battery_sound=self.battery_sound.isChecked())  # one setting for all games
        combined = dataclasses.replace(main, profiles=[dataclasses.replace(p, settings=None) for p in profiles])
        return config.parse(config.render(combined))

    def _store_form(self) -> bool:
        """Keep what's on the tabs before switching what is being edited."""
        try:
            everything = self.settings_from_window()
        except ConfigError as err:
            self._say(str(err), ERROR_STYLE)
            return False
        self.main_settings = dataclasses.replace(everything, profiles=[])
        self.profiles = list(everything.profiles)
        return True

    def _changed(self) -> None:
        if self._loading:
            return
        button, ratchet = self.gyro_button.currentData(), self.gyro_ratchet.currentData()
        pointing = self.pointer_button.currentData()
        for name, row in self.remap_rows.items():
            row.set_role("used as the gyro button" if name == button else
                         "used for gyro pause" if name == ratchet else
                         "used to point with a stick" if name == pointing else None)
        try:
            settings = self.settings_from_window()
        except ConfigError as err:
            self._say(str(err), ERROR_STYLE)
            self.save_button.setEnabled(False)
            return
        unsaved = config.render(settings) != self._saved_text
        self.save_button.setEnabled(unsaved)
        self.revert_button.setEnabled(unsaved and self._saved_text is not None)
        if unsaved:
            self._say("You have unsaved changes.", "")
        elif self.message.styleSheet() == ERROR_STYLE:
            self._say("", "")

    def _say(self, text: str, style: str) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(style)

    # ------------------------------------------------------------------ profiles

    def _game_label(self, profile: Profile) -> str:
        if self._steam_games is None:
            self._steam_games = dict(installed_steam_games())
        parts = [self._steam_games.get(app_id, f"Steam game {app_id}") for app_id in profile.steam_app_ids]
        return " or ".join(parts + list(profile.processes))

    def _show_profile_list(self, select: int) -> None:
        self._switching = True
        self.profile_select.clear()
        self.profile_select.addItem("Main settings (all games)")
        for profile in self.profiles:
            self.profile_select.addItem(f"Game profile: {profile.name}")
        self.profile_select.setCurrentIndex(select + 1)
        self._switching = False
        self.current = select
        self._show_current()

    def _show_current(self) -> None:
        self.delete_profile_button.setEnabled(self.current >= 0)
        if self.current < 0:
            self.defaults_button.setText("Restore defaults")
            self.profile_hint.setText("Used for every game, except where a game profile changes something.")
            self._show_settings(self.main_settings)
            return
        profile = self.profiles[self.current]
        self.defaults_button.setText("Match main settings")
        self.profile_hint.setText(f"Used while {self._game_label(profile)} is running. Settings you change here "
                                  "apply to that game only; everything else follows the main settings.")
        combined = dataclasses.replace(self.main_settings, profiles=[dataclasses.replace(profile, settings=None)])
        try:
            self._show_settings(config.parse(config.render(combined)).profiles[0].settings)
        except ConfigError as err:
            self._show_settings(self.main_settings)
            self._say(f"This profile no longer fits the main settings ({err}); it now shows the main settings.",
                      ERROR_STYLE)

    def _profile_selected(self, index: int) -> None:
        if self._switching or index < 0:
            return
        if not self._store_form():  # stay put so the mistake can be fixed first
            self._switching = True
            self.profile_select.setCurrentIndex(self.current + 1)
            self._switching = False
            return
        self.current = index - 1
        self._show_current()

    def add_profile(self, name: str, steam_app_id: int | None = None, process: str | None = None) -> str | None:
        """Add a game profile and start editing it. Returns a message instead if it can't be added."""
        name = name.strip()
        process = (process or "").strip().lower()
        if not name:
            return "Give the profile a name."
        if any(profile.name.lower() == name.lower() for profile in self.profiles):
            return f'There is already a profile called "{name}".'
        if not steam_app_id and not process:
            return "Choose a Steam game or type a program name."
        if not self._store_form():
            return self.message.text()
        self.profiles.append(Profile(name, (steam_app_id,) if steam_app_id else (), (process,) if process else ()))
        self._show_profile_list(len(self.profiles) - 1)
        return None

    def _ask_for_profile(self) -> None:
        if self._steam_games is None:
            self._steam_games = dict(installed_steam_games())
        dialog = AddProfileDialog(self, sorted(self._steam_games.items(), key=lambda game: game[1].lower()))
        while dialog.exec():
            error = self.add_profile(dialog.name.text(), dialog.steam_app_id(), dialog.process())
            if error is None:
                return
            dialog.error.setText(error)

    def delete_profile(self, confirm: bool = True) -> None:
        if self.current < 0:
            return
        name = self.profiles[self.current].name
        if confirm:
            answer = QMessageBox.question(self, "Delete profile", f'Delete the game profile "{name}"?')
            if answer != QMessageBox.StandardButton.Yes:
                return
        del self.profiles[self.current]  # the tabs showed this profile, so there's nothing else to keep
        self._show_profile_list(-1)

    # ------------------------------------------------------------------ actions

    def revert(self) -> None:
        try:
            settings = config.load(self.path)
            self._saved_text = config.render(settings) if os.path.exists(self.path) else None
            note = "" if self._saved_text is not None else "There's no settings file yet; Save creates one."
        except ConfigError as err:
            settings = Settings()
            self._saved_text = None
            note = f"Your settings file has a mistake ({err}). Showing the defaults; Save replaces the file and keeps a backup."
        self.main_settings = dataclasses.replace(settings, profiles=[])
        self.profiles = list(settings.profiles)
        self._show_profile_list(-1)
        if note:
            self._say(note, ERROR_STYLE if "mistake" in note else "")

    def restore_defaults(self) -> None:
        # for a profile, showing the main settings means it changes nothing any more
        self._show_settings(Settings() if self.current < 0 else self.main_settings)

    def save(self) -> bool:
        try:
            settings = self.settings_from_window()
            config.save(settings, self.path)
        except (ConfigError, OSError) as err:
            self._say(f"Couldn't save: {err}", ERROR_STYLE)
            return False
        self.main_settings = dataclasses.replace(settings, profiles=[])
        self.profiles = list(settings.profiles)
        self._saved_text = config.render(settings)
        self._changed()
        running = self.manage_service and self._systemctl("is-active") == "active"
        self._say("Saved. The controller uses the new settings within a second." if running or not self.manage_service
                  else "Saved. Start the background service to use them.", OK_STYLE)
        return True

    def run_setup(self) -> None:
        """Run the one-time setup in its own process, so the window stays responsive during the password prompt."""
        self.setup_button.setEnabled(False)
        self._say("Setting up… you may be asked for your password once, for the permissions rule.", HINT_STYLE)
        self._setup_process = QProcess(self)
        self._setup_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._setup_process.finished.connect(self._setup_finished)
        program, *arguments = launch.command("setup")
        self._setup_process.start(program, arguments)

    def _setup_finished(self, code: int, _status) -> None:
        output = bytes(self._setup_process.readAll()).decode(errors="replace").strip()
        self.setup_button.setEnabled(True)
        self._say(output or ("Setup finished." if code == 0 else "Setup didn't finish."), OK_STYLE if code == 0 else ERROR_STYLE)
        self.refresh_status()

    def refresh_status(self) -> None:
        current = status_file.read()
        connected = bool(current and current.get("connected") and current.get("firmware"))
        if connected:
            self._last_firmware = current["firmware"]
        text, warning = firmware_text(self._last_firmware, connected)
        self.firmware_label.setText(text)
        self.firmware_label.setStyleSheet(WARNING_STYLE if warning else "")
        plugged_in = bool(find_config_hidraws()) if current is None else False
        self.controller_status.setText(controller_summary(current, plugged_in))
        if not self.manage_service:
            self.service_status.setText("(not managed here)")
            for widget in (self.start_button, self.stop_button, self.autostart):
                widget.setEnabled(False)
            self.setup_button.setVisible(False)
            return
        active, enabled = self._systemctl("is-active"), self._systemctl("is-enabled")
        installed = enabled not in ("", "not-found")
        rule = install.rule_installed()
        text = ({"active": "Running", "activating": "Starting…", "failed": "Stopped after an error"}.get(active, "Stopped")
                if installed else "Not set up yet: click Set up")
        if installed and not rule:
            text += " · the permissions rule is missing or outdated: click Set up"
        self.service_status.setText(text)
        self.setup_button.setVisible(not (installed and rule))
        self.start_button.setEnabled(installed and active != "active")
        self.stop_button.setEnabled(installed and active == "active")
        self.autostart.setEnabled(installed)
        self.autostart.setChecked(enabled == "enabled")

    def _systemctl(self, command: str) -> str:
        try:
            result = subprocess.run(["systemctl", "--user", command, SERVICE], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip()

    def _service(self, command: str) -> None:
        try:
            result = subprocess.run(["systemctl", "--user", command, SERVICE], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as err:
            self._say(f"Couldn't {command} the background service: {err}", ERROR_STYLE)
            return
        if result.returncode != 0:
            self._say(f"Couldn't {command} the background service: {result.stderr.strip()}", ERROR_STYLE)
        self.refresh_status()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.save_button.isEnabled():
            buttons = QMessageBox.StandardButton
            answer = QMessageBox.question(self, "Unsaved changes", "Save your changes before closing?",
                                          buttons.Save | buttons.Discard | buttons.Cancel)
            if answer == buttons.Cancel or (answer == buttons.Save and not self.save()):
                event.ignore()
                return
        event.accept()


def instance_socket_path() -> str:
    return os.path.join(status_file.runtime_dir(), "settings.sock")


def forward_to_running_window(path: str, wait_s: float = 3.0) -> bool:
    """Ask an already open settings window to come to the front. Returns False if none answers."""
    # KDE only brings a window to the front with the activation token from the click that asked for it
    message = f"show {os.environ.get('XDG_ACTIVATION_TOKEN', '')}\n".encode()
    deadline = time.monotonic() + wait_s
    while True:
        try:
            with sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM) as client:
                client.settimeout(0.5)
                client.connect(path)
                client.sendall(message)
            return True
        except OSError:
            if time.monotonic() >= deadline:  # the first window may still be starting up
                return False
            time.sleep(0.1)


class SingleInstance:
    """Lets later launches bring this window to the front instead of opening another one."""

    def __init__(self, window: QMainWindow, path: str):
        self.window = window
        self.path = path
        self.notifier: QSocketNotifier | None = None
        self.server = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
        try:
            if os.path.exists(path):
                os.unlink(path)  # a socket left behind by a crash; only the lock holder gets here
            self.server.bind(path)
            self.server.listen(4)
            self.server.setblocking(False)
        except OSError as error:
            print(f"vader5-settings: later launches can't reach this window ({error})", file=sys.stderr)
            return
        self.notifier = QSocketNotifier(self.server.fileno(), QSocketNotifier.Type.Read)
        self.notifier.activated.connect(self._accept)

    def _accept(self, *_) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:  # nothing more waiting
                return
            with connection:
                connection.settimeout(0.5)
                try:
                    message = connection.recv(4096).decode(errors="replace").strip()
                except OSError:
                    continue
            if message.startswith("show"):
                self._show(message[len("show"):].strip())

    def _show(self, token: str) -> None:
        if token:
            os.environ["XDG_ACTIVATION_TOKEN"] = token  # Qt uses it for the next activation
        if self.window.isMinimized():
            self.window.showNormal()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def close(self) -> None:
        if self.notifier is not None:
            self.notifier.setEnabled(False)
        self.server.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def start_tray(spawn=subprocess.Popen) -> bool:
    """Start the tray icon along with the window. If it's already running, the new one quits by itself."""
    return launch.start_detached(launch.command("tray"), spawn)


def main(argv: list[str] | None = None) -> int:
    app = QApplication([sys.argv[0], *(sys.argv[1:] if argv is None else argv)])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("vader5-settings")
    app.setWindowIcon(QIcon(launch.ICON_FILE))
    start_tray()
    lock = status_file.single_instance_lock("vader5-settings")
    if lock is None:  # already open: bring that window to the front instead
        return 0 if forward_to_running_window(instance_socket_path()) else 1
    window = SettingsWindow()
    instance = SingleInstance(window, instance_socket_path())  # noqa: F841 - kept alive with the window
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
