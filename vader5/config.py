"""Settings file: ~/.config/vader5/config.toml.

vader5-pad reads it at start and again whenever the file is saved, so changes apply within about a
second without a restart. If the file has a mistake, the error is logged and the previous settings
stay in use. The settings window (vader5-settings) writes it with save().

    ./vader5-config create   write a starter file with explanations (if there isn't one)
    ./vader5-config check    show mistakes, or the settings that will be used
    ./vader5-config path     print where the file is
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from typing import NamedTuple

from . import keyboard_mouse, protocol
from .gyro import GyroSettings
from .virtual_pad import BUTTON_CODES

NONE = "NONE"  # remap target that turns a button off
ALIASES = {"VIEW": "SELECT", "BACK": "SELECT", "MENU": "START", "GUIDE": "HOME"}


class ConfigError(ValueError):
    """The settings file has a mistake; the message says where."""


class KeyCombo(NamedTuple):
    text: str  # as written in the settings file, e.g. "key:ctrl+c"
    codes: tuple[int, ...]  # keyboard key / mouse button codes, in the order they're pressed


@dataclass
class Settings:
    gyro: GyroSettings = field(default_factory=GyroSettings)
    left_deadzone: float = 0.0  # fraction of full stick travel ignored around the center
    right_deadzone: float = 0.0
    remap: dict[str, str] = field(default_factory=dict)  # physical button -> controller button, or NONE
    key_remap: dict[str, KeyCombo] = field(default_factory=dict)  # physical button -> keys/mouse buttons

    def output_buttons(self, pressed: frozenset[str]) -> frozenset[str]:
        """The controller buttons games should see for these physical presses."""
        out = set()
        for name in pressed:
            if name in (self.gyro.button, self.gyro.ratchet) or name in self.key_remap:
                continue  # gyro buttons never reach games; key remaps go to the keyboard instead
            target = self.remap.get(name, name)
            if target != NONE:
                out.add(target)
        return frozenset(out)

    def keys_for(self, pressed: frozenset[str]) -> tuple[int, ...]:
        """Keyboard keys and mouse buttons to hold down for these physical presses, in press order."""
        codes: list[int] = []
        for name in protocol.BUTTON_NAMES:  # a fixed order, so holding two remapped buttons is predictable
            if name in pressed and name in self.key_remap and name not in (self.gyro.button, self.gyro.ratchet):
                codes += [code for code in self.key_remap[name].codes if code not in codes]
        return tuple(codes)

    def for_games(self, state: protocol.InputState) -> protocol.InputState:
        """The input state as games should see it: remapped buttons and stick deadzones applied."""
        return dataclasses.replace(
            state,
            buttons=self.output_buttons(state.buttons),
            left_stick=apply_deadzone(*state.left_stick, self.left_deadzone),
            right_stick=apply_deadzone(*state.right_stick, self.right_deadzone),
        )


def apply_deadzone(x: int, y: int, deadzone: float) -> tuple[int, int]:
    """Round deadzone: ignore movement near the center and stretch the rest back to full range."""
    if deadzone <= 0:
        return x, y
    magnitude = math.hypot(x, y) / 32767
    if magnitude <= deadzone:
        return 0, 0
    scale = min(1.0, (magnitude - deadzone) / (1 - deadzone)) / magnitude
    return _clamp_axis(round(x * scale)), _clamp_axis(round(y * scale))


def _clamp_axis(value: int) -> int:
    return max(-32768, min(32767, value))


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "vader5", "config.toml")


def load(path: str | None = None) -> Settings:
    """Settings from the file, or the defaults if there's no file. Raises ConfigError for mistakes."""
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return Settings()
    except OSError as err:
        raise ConfigError(f"couldn't read the file: {err.strerror}") from None
    return parse(text)


def parse(text: str) -> Settings:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"the file isn't valid TOML: {err}") from None
    _check_keys(data, {"gyro", "sticks", "remap"}, "the file")
    settings = Settings()

    gyro = _table(data, "gyro")
    _check_keys(gyro, {"button", "ratchet", "sensitivity", "horizontal_scale", "vertical_scale",
                       "invert_x", "invert_y", "tightening"}, "[gyro]")
    if "button" in gyro:
        settings.gyro.button = _button(gyro["button"], "[gyro] button", protocol.BUTTON_NAMES)
    if "ratchet" in gyro:
        settings.gyro.ratchet = _button(gyro["ratchet"], "[gyro] ratchet", (*protocol.BUTTON_NAMES, NONE))
    if settings.gyro.ratchet == "TURBO":
        raise ConfigError("[gyro] ratchet can't be TURBO: Turbo only sends a short pulse, so it can't be held")
    if settings.gyro.ratchet != NONE and settings.gyro.ratchet == settings.gyro.button:
        raise ConfigError("[gyro] ratchet and button can't be the same button")
    if "sensitivity" in gyro:
        settings.gyro.sensitivity = _number(gyro["sensitivity"], "[gyro] sensitivity", 0.0, 1000.0)
    for key in ("horizontal_scale", "vertical_scale"):
        if key in gyro:
            setattr(settings.gyro, key, _number(gyro[key], f"[gyro] {key}", 0.0, 10.0))
    for key in ("invert_x", "invert_y"):
        if key in gyro:
            setattr(settings.gyro, key, _boolean(gyro[key], f"[gyro] {key}"))
    if "tightening" in gyro:
        settings.gyro.tightening_dps = _number(gyro["tightening"], "[gyro] tightening", 0.0, 20.0)

    sticks = _table(data, "sticks")
    _check_keys(sticks, {"left_deadzone", "right_deadzone"}, "[sticks]")
    for key in ("left_deadzone", "right_deadzone"):
        if key in sticks:
            setattr(settings, key, _number(sticks[key], f"[sticks] {key}", 0.0, 0.9))

    for source, target in _table(data, "remap").items():
        name = _button(source, "[remap]", protocol.BUTTON_NAMES)
        where = f"[remap] {source}"
        if isinstance(target, str) and ":" in target:
            settings.key_remap[name] = key_combo(target, where)
            settings.remap.pop(name, None)
        else:
            settings.remap[name] = _button(target, where, (*BUTTON_CODES, NONE))
            settings.key_remap.pop(name, None)
    return settings


def render(settings: Settings) -> str:
    """The settings as a complete settings file, with explanations."""
    gyro = settings.gyro
    lines = [
        "# Vader 5 Pro settings. vader5-pad applies changes about a second after you save.",
        "# Edit this file by hand or with the settings window (vader5-settings).",
        "#",
        "# Button names (upper or lower case):",
        "#   A B X Y LB RB L3 R3 SELECT START HOME UP DOWN LEFT RIGHT",
        "#   M1 M2 M3 M4 C Z LM RM FN TURBO",
        "#   LT RT (the triggers' full-press click; only usable as a remap source or the gyro button)",
        "",
        "[gyro]",
        "# Button that turns gyro aiming on and off. It isn't sent to games.",
        f"button = {_quote(gyro.button)}",
        "# Hold this button to pause gyro aiming while you bring your hands back to center, like lifting a",
        '# mouse off the desk. It isn\'t sent to games. "NONE" = no pause button. Turbo can\'t be held, so it',
        "# can't be used here.",
        f"ratchet = {_quote(gyro.ratchet)}",
        "# Mouse movement per degree the controller turns. Higher is faster.",
        f"sensitivity = {float(gyro.sensitivity)!r}",
        "# Extra speed for one direction on top of sensitivity: 1.25 = a quarter more, 0.8 = a fifth less.",
        f"horizontal_scale = {float(gyro.horizontal_scale)!r}",
        f"vertical_scale = {float(gyro.vertical_scale)!r}",
        f"invert_x = {str(gyro.invert_x).lower()}",
        f"invert_y = {str(gyro.invert_y).lower()}",
        "# Rotation slower than this many degrees per second is scaled down to steady your hands. 0 = off.",
        f"tightening = {float(gyro.tightening_dps)!r}",
        "",
        "[sticks]",
        "# How much of each stick's travel around the center is ignored, from 0.0 (off) to 0.9.",
        "# Games have their own deadzones, so leave these at 0.0 unless a stick drifts.",
        f"left_deadzone = {float(settings.left_deadzone)!r}",
        f"right_deadzone = {float(settings.right_deadzone)!r}",
        "",
        "[remap]",
        "# physical button = what it sends instead. It can be:",
        '#   another controller button      M1 = "A"',
        '#   a keyboard key or combination  M2 = "key:space"      M3 = "key:ctrl+c"',
        '#   a mouse button                 M4 = "mouse:right"    (left, right, middle, back, forward)',
        '#   nothing                        RM = "NONE"',
        "# Key names: a-z, 0-9, space, enter, esc, tab, backspace, ctrl, shift, alt, super, up, down, left,",
        '# right, f1-f24, and everything else in /usr/include/linux/input-event-codes.h without "KEY_".',
    ]
    for name in protocol.BUTTON_NAMES:
        if name in settings.key_remap:
            lines.append(f"{name} = {_quote(settings.key_remap[name].text)}")
        elif name in settings.remap:
            lines.append(f"{name} = {_quote(settings.remap[name])}")
    return "\n".join(lines) + "\n"


def save(settings: Settings, path: str | None = None) -> str:
    """Write the settings file, keeping the previous one as config.toml.bak. Returns the path."""
    path = path or config_path()
    text = render(settings)
    if parse(text) != settings:
        raise ConfigError("these settings couldn't be written without changing them")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(temporary, path)  # vader5-pad never sees a half-written file
    return path


def _quote(text: str) -> str:
    return json.dumps(text)  # a valid TOML basic string for our button names and key combinations


def _table(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' should be a section like [{name}]")
    return value


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            raise ConfigError(f"unknown setting '{key}' in {where} (allowed: {', '.join(sorted(allowed))})")


def _button(value, where: str, allowed) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: a button name must be text in quotes, like \"A\"")
    name = ALIASES.get(value.strip().upper(), value.strip().upper())
    if name not in allowed:
        raise ConfigError(f"{where}: unknown button '{value}' (use one of: {' '.join(allowed)})")
    return name


def key_combo(text: str, where: str) -> KeyCombo:
    """Parse "key:space", "key:ctrl+c" or "mouse:right". A part without a prefix uses the previous one."""
    codes: list[int] = []
    kind = None
    for part in text.split("+"):
        part = part.strip()
        if ":" in part:
            kind, _, part = part.partition(":")
            kind, part = kind.strip().lower(), part.strip()
        if kind not in ("key", "mouse"):
            raise ConfigError(f'{where}: "{text}" should start with key: or mouse:, like "key:space" or "mouse:right"')
        if not part:
            raise ConfigError(f'{where}: "{text}" is missing a key name')
        if kind == "key":
            code = keyboard_mouse.key_code(part)
            if code is None:
                raise ConfigError(
                    f"{where}: unknown key '{part}' (examples: a, 1, space, enter, esc, tab, ctrl, shift, alt, "
                    f"f1, f13; all names are in /usr/include/linux/input-event-codes.h without KEY_)"
                )
        else:
            code = keyboard_mouse.mouse_code(part)
            if code is None:
                raise ConfigError(f"{where}: unknown mouse button '{part}' (use left, right, middle, back or forward)")
        if code not in codes:
            codes.append(code)
    return KeyCombo(text.strip(), tuple(codes))


def _number(value, where: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} should be a number")
    if not low <= value <= high:
        raise ConfigError(f"{where} should be between {low:g} and {high:g}, not {value}")
    return float(value)


def _boolean(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} should be true or false")
    return value


STARTER = render(Settings())


class ConfigWatcher:
    """Re-reads the settings file when it changes, keeping the last good settings after a mistake."""

    CHECK_S = 1.0

    def __init__(self, path: str | None = None, log=print):
        self.path = path or config_path()
        self.settings = Settings()
        self._log = log
        self._stamp: object = object()  # never equal to a real stamp, so the first check loads
        self._next_check = 0.0

    def check(self, now: float) -> bool:
        """Reload if the file changed (looked at most once a second). Returns True if settings changed."""
        if now < self._next_check:
            return False
        self._next_check = now + self.CHECK_S
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        try:
            settings = load(self.path)
        except ConfigError as err:
            self._log(f"settings file has a mistake, so the previous settings stay in use: {err} ({self.path})")
            return False
        self.settings = settings
        self._log(f"settings loaded from {self.path}" if stamp else f"no settings file at {self.path}, using defaults")
        return True

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return None
        return info.st_mtime_ns, info.st_size


def describe(settings: Settings) -> str:
    gyro = settings.gyro
    remaps = [f"{source} -> {target}" for source, target in settings.remap.items()]
    remaps += [f"{source} -> {combo.text}" for source, combo in settings.key_remap.items()]
    return "\n".join([
        f"gyro: toggle {gyro.button}, ratchet {gyro.ratchet}, sensitivity {gyro.sensitivity:g} "
        f"(horizontal x{gyro.horizontal_scale:g}, vertical x{gyro.vertical_scale:g}), invert x {str(gyro.invert_x).lower()}, "
        f"invert y {str(gyro.invert_y).lower()}, tightening {gyro.tightening_dps:g}",
        f"sticks: left deadzone {settings.left_deadzone:g}, right deadzone {settings.right_deadzone:g}",
        "remap: " + (", ".join(sorted(remaps)) or "none"),
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or check the Vader 5 Pro settings file.")
    parser.add_argument("command", nargs="?", default="check", choices=("create", "check", "path"))
    args = parser.parse_args(argv)
    path = config_path()

    if args.command == "path":
        print(path)
        return 0
    if args.command == "create":
        if os.path.exists(path):
            print(f"{path} already exists, so it was left as it is.")
            return 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "x", encoding="utf-8") as f:
            f.write(STARTER)
        print(f"Created {path}\nEdit it and save; vader5-pad applies changes within about a second.")
        return 0

    if not os.path.exists(path):
        print(f"No settings file at {path}, so the defaults are used. Create one with: ./vader5-config create")
    try:
        settings = load(path)
    except ConfigError as err:
        print(f"Problem in {path}:\n  {err}", file=sys.stderr)
        return 1
    print(describe(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
