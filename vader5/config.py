"""Settings file: ~/.config/vader5/config.toml.

vader5-pad reads it at start and again whenever the file is saved, so changes apply within about a
second without a restart. If the file has a mistake, the error is logged and the previous settings
stay in use.

    ./vader5-config create   write a starter file with explanations (if there isn't one)
    ./vader5-config check    show mistakes, or the settings that will be used
    ./vader5-config path     print where the file is
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import sys
import tomllib
from dataclasses import dataclass, field

from . import protocol
from .gyro import GyroSettings
from .virtual_pad import BUTTON_CODES

NONE = "NONE"  # remap target that turns a button off
ALIASES = {"VIEW": "SELECT", "BACK": "SELECT", "MENU": "START", "GUIDE": "HOME"}

STARTER = """\
# Vader 5 Pro settings. vader5-pad applies changes about a second after you save.
#
# Button names (upper or lower case):
#   A B X Y LB RB L3 R3 SELECT START HOME UP DOWN LEFT RIGHT
#   M1 M2 M3 M4 C Z LM RM FN TURBO
#   LT RT (the triggers' full-press click; only usable as a remap source or the gyro button)

[gyro]
# Button that turns gyro aiming on and off. It isn't sent to games.
button = "TURBO"
# Mouse movement per degree the controller turns. Higher is faster.
sensitivity = 15.0
invert_x = false
invert_y = false
# Rotation slower than this many degrees per second is scaled down to steady your hands. 0 = off.
tightening = 1.0

[sticks]
# How much of each stick's travel around the center is ignored, from 0.0 (off) to 0.9.
# Games have their own deadzones, so leave these at 0.0 unless a stick drifts.
left_deadzone = 0.0
right_deadzone = 0.0

[remap]
# physical button = the button it sends, or "NONE" to turn it off. For example:
# M1 = "A"
# M2 = "NONE"
"""


class ConfigError(ValueError):
    """The settings file has a mistake; the message says where."""


@dataclass
class Settings:
    gyro: GyroSettings = field(default_factory=GyroSettings)
    left_deadzone: float = 0.0  # fraction of full stick travel ignored around the center
    right_deadzone: float = 0.0
    remap: dict[str, str] = field(default_factory=dict)  # physical button -> button it sends, or NONE

    def output_buttons(self, pressed: frozenset[str]) -> frozenset[str]:
        """The buttons games should see for these physical presses."""
        out = set()
        for name in pressed:
            if name == self.gyro.button:
                continue  # the gyro toggle never reaches games
            target = self.remap.get(name, name)
            if target != NONE:
                out.add(target)
        return frozenset(out)

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
    _check_keys(gyro, {"button", "sensitivity", "invert_x", "invert_y", "tightening"}, "[gyro]")
    if "button" in gyro:
        settings.gyro.button = _button(gyro["button"], "[gyro] button", protocol.BUTTON_NAMES)
    if "sensitivity" in gyro:
        settings.gyro.sensitivity = _number(gyro["sensitivity"], "[gyro] sensitivity", 0.0, 1000.0)
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
        settings.remap[name] = _button(target, f"[remap] {source}", (*BUTTON_CODES, NONE))
    return settings


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
    return "\n".join([
        f"gyro: toggle {gyro.button}, sensitivity {gyro.sensitivity:g}, invert x {str(gyro.invert_x).lower()}, "
        f"invert y {str(gyro.invert_y).lower()}, tightening {gyro.tightening_dps:g}",
        f"sticks: left deadzone {settings.left_deadzone:g}, right deadzone {settings.right_deadzone:g}",
        "remap: " + (", ".join(f"{source} -> {target}" for source, target in sorted(settings.remap.items())) or "none"),
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
