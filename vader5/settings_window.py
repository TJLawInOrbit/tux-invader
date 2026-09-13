"""Settings window for the Vader 5 Pro app.

    ./vader5-settings

Edits ~/.config/vader5/config.toml, including per-game profiles; vader5-pad applies saved changes
within about a second. It also shows whether the controller and the background service are running,
and can start or stop it.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider,
    QTabWidget, QVBoxLayout, QWidget,
)

from . import config
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
NOT_CONNECTED = "Not connected: turn the controller on, or plug in the cable or dongle"


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
        self.setWindowTitle("Vader 5 Pro Settings")
        self.resize(780, 820)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_status())
        layout.addLayout(self._build_profile_bar())
        self.profile_hint = hint("")
        layout.addWidget(self.profile_hint)
        tabs = QTabWidget()
        tabs.addTab(self._build_gyro_tab(), "Gyro")
        tabs.addTab(self._build_sticks_tab(), "Sticks")
        tabs.addTab(self._build_buttons_tab(), "Buttons")
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

    def _build_status(self) -> QGroupBox:
        box = QGroupBox("Status")
        grid = QGridLayout(box)
        self.controller_status = QLabel()
        self.controller_status.setWordWrap(True)
        self.service_status = QLabel()
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.autostart = QCheckBox("Start automatically when I log in")
        grid.addWidget(QLabel("Controller:"), 0, 0)
        grid.addWidget(self.controller_status, 0, 1, 1, 3)
        grid.addWidget(QLabel("Background service:"), 1, 0)
        grid.addWidget(self.service_status, 1, 1)
        grid.addWidget(self.start_button, 1, 2)
        grid.addWidget(self.stop_button, 1, 3)
        grid.addWidget(self.autostart, 2, 1, 1, 3)
        grid.setColumnStretch(1, 1)
        self.start_button.clicked.connect(lambda: self._service("start"))
        self.stop_button.clicked.connect(lambda: self._service("stop"))
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
        form.addRow(hint("Press the on/off button on the controller to start gyro aiming: a short light buzz "
                         "means on, a longer heavy buzz means off. Turning the controller then moves your aim."))
        self.gyro_button = QComboBox()
        for name in UI_ORDER:
            self.gyro_button.addItem(BUTTON_LABELS[name], name)
        self.gyro_ratchet = QComboBox()
        self.gyro_ratchet.addItem("None", NONE)
        for name in UI_ORDER:
            if name != "TURBO":
                self.gyro_ratchet.addItem(BUTTON_LABELS[name], name)
        self.sensitivity = SliderSpin(0, 1000, 0.5, 1, slider_high=50)
        self.horizontal_scale = SliderSpin(0, 10, 0.05, 2, slider_high=3)
        self.vertical_scale = SliderSpin(0, 10, 0.05, 2, slider_high=3)
        self.invert_x = QCheckBox("Invert left / right")
        self.invert_y = QCheckBox("Invert up / down")
        self.tightening = SliderSpin(0, 20, 0.1, 1, slider_high=5)

        form.addRow("On / off button:", self.gyro_button)
        form.addRow("Pause while held (ratchet):", self.gyro_ratchet)
        form.addRow("", hint("Hold it to bring your hands back to center without moving your aim."))
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
        for widget in (self.gyro_button, self.gyro_ratchet):
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
        for widget in (self.left_deadzone, self.right_deadzone):
            widget.valueChanged.connect(lambda _: self._changed())
        return page

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

    # ------------------------------------------------------------------ settings <-> window

    def _show_settings(self, settings: Settings) -> None:
        self._loading = True
        gyro = settings.gyro
        self.gyro_button.setCurrentIndex(self.gyro_button.findData(gyro.button))
        self.gyro_ratchet.setCurrentIndex(max(0, self.gyro_ratchet.findData(gyro.ratchet)))
        self.sensitivity.setValue(gyro.sensitivity)
        self.horizontal_scale.setValue(gyro.horizontal_scale)
        self.vertical_scale.setValue(gyro.vertical_scale)
        self.invert_x.setChecked(gyro.invert_x)
        self.invert_y.setChecked(gyro.invert_y)
        self.tightening.setValue(gyro.tightening_dps)
        self.left_deadzone.setValue(settings.left_deadzone)
        self.right_deadzone.setValue(settings.right_deadzone)
        for name, row in self.remap_rows.items():
            row.set_target(settings.target(name))
        self._loading = False
        self._changed()

    def _form_settings(self) -> Settings:
        """The settings shown on the tabs (without profiles), checked like the settings file. Raises ConfigError."""
        settings = Settings()
        gyro = settings.gyro
        gyro.button = self.gyro_button.currentData()
        gyro.ratchet = self.gyro_ratchet.currentData()
        gyro.sensitivity = self.sensitivity.value()
        gyro.horizontal_scale = self.horizontal_scale.value()
        gyro.vertical_scale = self.vertical_scale.value()
        gyro.invert_x = self.invert_x.isChecked()
        gyro.invert_y = self.invert_y.isChecked()
        gyro.tightening_dps = self.tightening.value()
        settings.left_deadzone = self.left_deadzone.value()
        settings.right_deadzone = self.right_deadzone.value()
        for name, row in self.remap_rows.items():
            target = row.target()
            if target is None:
                continue
            if ":" in target:
                settings.key_remap[name] = KeyCombo(target, ())
            else:
                settings.remap[name] = target
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
        for name, row in self.remap_rows.items():
            row.set_role("used for gyro on / off" if name == button else "used for gyro pause" if name == ratchet else None)
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

    def refresh_status(self) -> None:
        current = status_file.read()
        plugged_in = bool(find_config_hidraws()) if current is None else False
        self.controller_status.setText(controller_summary(current, plugged_in))
        if not self.manage_service:
            self.service_status.setText("(not managed here)")
            for widget in (self.start_button, self.stop_button, self.autostart):
                widget.setEnabled(False)
            return
        active, enabled = self._systemctl("is-active"), self._systemctl("is-enabled")
        installed = enabled not in ("", "not-found")
        self.service_status.setText(
            {"active": "Running", "activating": "Starting…", "failed": "Stopped after an error"}.get(active, "Stopped")
            if installed else "Not installed (run ./vader5-service install)"
        )
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
    deadline = time.monotonic() + wait_s
    while True:
        socket = QLocalSocket()
        socket.connectToServer(path)
        if socket.waitForConnected(300):
            # KDE only brings a window to the front with the activation token from the click that asked for it
            socket.write(f"show {os.environ.get('XDG_ACTIVATION_TOKEN', '')}\n".encode())
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()
            return True
        if time.monotonic() >= deadline:  # the first window may still be starting up
            return False
        time.sleep(0.1)


class SingleInstance:
    """Lets later launches bring this window to the front instead of opening another one."""

    def __init__(self, window: QMainWindow, path: str):
        self.window = window
        QLocalServer.removeServer(path)  # a socket left behind by a crash; only the lock holder gets here
        self.server = QLocalServer()
        if not self.server.listen(path):
            print(f"vader5-settings: later launches can't reach this window ({self.server.errorString()})",
                  file=sys.stderr)
        self.server.newConnection.connect(self._accept)

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: self._handle(s))
            if socket.bytesAvailable():
                self._handle(socket)

    def _handle(self, socket: QLocalSocket) -> None:
        message = bytes(socket.readAll()).decode(errors="replace").strip()
        if message.startswith("show"):
            token = message[len("show"):].strip()
            if token:
                os.environ["XDG_ACTIVATION_TOKEN"] = token  # Qt uses it for the next activation
            if self.window.isMinimized():
                self.window.showNormal()
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
        socket.disconnectFromServer()

    def close(self) -> None:
        self.server.close()


def main(argv: list[str] | None = None) -> int:
    app = QApplication([sys.argv[0], *(sys.argv[1:] if argv is None else argv)])
    app.setApplicationName("Vader 5 Pro Settings")
    app.setDesktopFileName("vader5-settings")
    lock = status_file.single_instance_lock("vader5-settings")
    if lock is None:  # already open: bring that window to the front instead
        return 0 if forward_to_running_window(instance_socket_path()) else 1
    window = SettingsWindow()
    instance = SingleInstance(window, instance_socket_path())  # noqa: F841 - kept alive with the window
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
