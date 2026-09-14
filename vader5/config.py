"""Settings file: ~/.config/vader5/config.toml.

vader5-pad reads it at start and again whenever the file is saved, so changes apply within about a
second without a restart. If the file has a mistake, the error is logged and the previous settings
stay in use. The settings window (vader5-settings) writes it with save(). Per-game profiles
([[profile]] sections) only list what differs from the main settings.

    ./vader5-config create   write a starter file with explanations (if there isn't one)
    ./vader5-config check    show mistakes, or the settings that will be used
    ./vader5-config path     print where the file is
"""

from __future__ import annotations

import argparse
import copy
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
from . import led as lighting
from .gyro import GyroSettings
from .virtual_pad import BUTTON_CODES

NONE = "NONE"  # remap target that turns a button off
ALIASES = {"VIEW": "SELECT", "BACK": "SELECT", "MENU": "START", "GUIDE": "HOME"}
SECTIONS = ("gyro", "sticks", "remap", "led")
GYRO_SETTINGS = {  # name in the file -> GyroSettings attribute
    "button": "button", "ratchet": "ratchet", "sensitivity": "sensitivity",
    "horizontal_scale": "horizontal_scale", "vertical_scale": "vertical_scale",
    "invert_x": "invert_x", "invert_y": "invert_y", "tightening": "tightening_dps",
}
STICK_SETTINGS = ("left_deadzone", "right_deadzone")
LED_SETTINGS = ("effect", "colors", "brightness", "speed")
PROFILE_KEYS = {"name", "steam_app_id", "process", *SECTIONS}


class ConfigError(ValueError):
    """The settings file has a mistake; the message says where."""


class KeyCombo(NamedTuple):
    text: str  # as written in the settings file, e.g. "key:ctrl+c"
    codes: tuple[int, ...]  # keyboard key / mouse button codes, in the order they're pressed


@dataclass
class Profile:
    """Settings for one game: only what differs from the main settings."""

    name: str
    steam_app_ids: tuple[int, ...] = ()
    processes: tuple[str, ...] = ()  # lower-case program names, e.g. "game.exe"
    overrides: dict[str, dict] = field(default_factory=dict)  # "gyro"/"sticks"/"remap" -> {setting: value}
    settings: Settings | None = field(default=None, compare=False, repr=False)  # main settings plus overrides

    def matches(self, process) -> bool:
        """Whether a running process (games.RunningProcess) belongs to this profile's game."""
        return process.steam_app_id in self.steam_app_ids or not set(self.processes).isdisjoint(process.names)

    def describe_game(self) -> str:
        return ", ".join([f"Steam game {app_id}" for app_id in self.steam_app_ids] + list(self.processes))


@dataclass
class Settings:
    gyro: GyroSettings = field(default_factory=GyroSettings)
    left_deadzone: float = 0.0  # fraction of full stick travel ignored around the center
    right_deadzone: float = 0.0
    remap: dict[str, str] = field(default_factory=dict)  # physical button -> controller button, or NONE
    key_remap: dict[str, KeyCombo] = field(default_factory=dict)  # physical button -> keys/mouse buttons
    led: lighting.LedSettings = field(default_factory=lighting.LedSettings)
    profiles: list[Profile] = field(default_factory=list)

    def for_profile(self, name: str | None) -> Settings:
        """The settings to use while that profile's game runs (these settings if there's no such profile)."""
        for profile in self.profiles:
            if profile.name == name and profile.settings is not None:
                return profile.settings
        return self

    def target(self, name: str) -> str | None:
        """What a button is remapped to, as written in the file; None if it isn't remapped."""
        if name in self.key_remap:
            return self.key_remap[name].text
        return self.remap.get(name)

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
    _check_keys(data, {*SECTIONS, "profile"}, "the file")
    settings = Settings()
    _apply_sections(settings, data, "")
    profiles = data.get("profile", [])
    if not isinstance(profiles, list):
        raise ConfigError("profiles should be written as [[profile]] sections")
    for index, table in enumerate(profiles, 1):
        settings.profiles.append(_parse_profile(table, settings, index))
    return settings


def _apply_sections(settings: Settings, tables: dict, prefix: str) -> None:
    """Apply [gyro], [sticks] and [remap] settings on top of `settings`. Raises ConfigError."""
    gyro = _table(tables, "gyro", prefix)
    _check_keys(gyro, set(GYRO_SETTINGS), f"{prefix}[gyro]")
    if "button" in gyro:
        settings.gyro.button = _button(gyro["button"], f"{prefix}[gyro] button", protocol.BUTTON_NAMES)
    if "ratchet" in gyro:
        settings.gyro.ratchet = _button(gyro["ratchet"], f"{prefix}[gyro] ratchet", (*protocol.BUTTON_NAMES, NONE))
    if settings.gyro.ratchet == "TURBO":
        raise ConfigError(f"{prefix}[gyro] ratchet can't be TURBO: Turbo only sends a short pulse, so it can't be held")
    if settings.gyro.ratchet != NONE and settings.gyro.ratchet == settings.gyro.button:
        raise ConfigError(f"{prefix}[gyro] ratchet and button can't be the same button")
    if "sensitivity" in gyro:
        settings.gyro.sensitivity = _number(gyro["sensitivity"], f"{prefix}[gyro] sensitivity", 0.0, 1000.0)
    for key in ("horizontal_scale", "vertical_scale"):
        if key in gyro:
            setattr(settings.gyro, key, _number(gyro[key], f"{prefix}[gyro] {key}", 0.0, 10.0))
    for key in ("invert_x", "invert_y"):
        if key in gyro:
            setattr(settings.gyro, key, _boolean(gyro[key], f"{prefix}[gyro] {key}"))
    if "tightening" in gyro:
        settings.gyro.tightening_dps = _number(gyro["tightening"], f"{prefix}[gyro] tightening", 0.0, 20.0)

    sticks = _table(tables, "sticks", prefix)
    _check_keys(sticks, set(STICK_SETTINGS), f"{prefix}[sticks]")
    for key in STICK_SETTINGS:
        if key in sticks:
            setattr(settings, key, _number(sticks[key], f"{prefix}[sticks] {key}", 0.0, 0.9))

    for source, target in _table(tables, "remap", prefix).items():
        name = _button(source, f"{prefix}[remap]", protocol.BUTTON_NAMES)
        where = f"{prefix}[remap] {source}"
        if isinstance(target, str) and ":" in target:
            settings.key_remap[name] = key_combo(target, where)
            settings.remap.pop(name, None)
        else:
            settings.remap[name] = _button(target, where, (*BUTTON_CODES, NONE))
            settings.key_remap.pop(name, None)

    lights = _table(tables, "led", prefix)
    _check_keys(lights, set(LED_SETTINGS), f"{prefix}[led]")
    changes = {}
    if "effect" in lights:
        effect = lights["effect"]
        if not isinstance(effect, str) or effect.strip().lower() not in lighting.EFFECTS:
            raise ConfigError(f"{prefix}[led] effect: unknown effect {effect!r} "
                              f"(use one of: {' '.join(lighting.EFFECTS)})")
        changes["effect"] = effect.strip().lower()
    if "colors" in lights:
        changes["colors"] = _colors(lights["colors"], f"{prefix}[led] colors")
    if "brightness" in lights:
        changes["brightness"] = _integer(lights["brightness"], f"{prefix}[led] brightness", 0, 100)
    if "speed" in lights:
        changes["speed"] = _integer(lights["speed"], f"{prefix}[led] speed", 1, 10)
    if changes:
        settings.led = dataclasses.replace(settings.led, **changes)


def _led_value(lights: lighting.LedSettings, key: str):
    """An LED setting as written in the file."""
    value = getattr(lights, key)
    return [lighting.color_text(color) for color in value] if key == "colors" else value


def _parse_profile(table, main: Settings, index: int) -> Profile:
    if not isinstance(table, dict):
        raise ConfigError(f"profile {index} should be a [[profile]] section")
    name = table.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f'profile {index} needs a name, like name = "Street Fighter 6"')
    name = name.strip()
    where = f'profile "{name}"'
    _check_keys(table, PROFILE_KEYS, where)
    if any(existing.name.lower() == name.lower() for existing in main.profiles):
        raise ConfigError(f'there are two profiles called "{name}"')
    app_ids = tuple(_int_list(table.get("steam_app_id", []), f"{where} steam_app_id"))
    processes = tuple(process.lower() for process in _text_list(table.get("process", []), f"{where} process"))
    if not app_ids and not processes:
        raise ConfigError(f"{where} needs steam_app_id or process, so it knows which game it's for")
    effective = copy.deepcopy(dataclasses.replace(main, profiles=[]))
    _apply_sections(effective, table, f"{where} ")
    return Profile(name, app_ids, processes, _overrides_as_written(table, effective), effective)


def _overrides_as_written(tables: dict, effective: Settings) -> dict[str, dict]:
    """A profile's own settings in their tidy form (checked values, standard names), for writing back."""
    gyro = {key: getattr(effective.gyro, GYRO_SETTINGS[key]) for key in _table(tables, "gyro")}
    sticks = {key: getattr(effective, key) for key in _table(tables, "sticks")}
    remap = {}
    for source in _table(tables, "remap"):
        name = ALIASES.get(source.strip().upper(), source.strip().upper())
        remap[name] = effective.target(name)
    lights = {key: _led_value(effective.led, key) for key in _table(tables, "led")}
    return {section: values for section, values in
            (("gyro", gyro), ("sticks", sticks), ("remap", remap), ("led", lights)) if values}


def overrides_between(main: Settings, effective: Settings) -> dict[str, dict]:
    """The profile settings that turn the main settings into `effective`."""
    gyro = {key: getattr(effective.gyro, attr) for key, attr in GYRO_SETTINGS.items()
            if getattr(effective.gyro, attr) != getattr(main.gyro, attr)}
    sticks = {key: getattr(effective, key) for key in STICK_SETTINGS if getattr(effective, key) != getattr(main, key)}
    remap = {}
    for name in protocol.BUTTON_NAMES:
        wanted = effective.target(name)
        if wanted != main.target(name):
            # "back to itself" has to be spelled out, or the main remap would still apply
            remap[name] = wanted if wanted is not None else (name if name in BUTTON_CODES else NONE)
    lights = {key: _led_value(effective.led, key) for key in LED_SETTINGS
              if getattr(effective.led, key) != getattr(main.led, key)}
    return {section: values for section, values in
            (("gyro", gyro), ("sticks", sticks), ("remap", remap), ("led", lights)) if values}


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
        "[led]",
        '# What the LED strip shows. "controller" leaves the lights stored on the controller alone.',
        "# Effects: " + " ".join(lighting.EFFECTS),
        "# vader5-pad sends these whenever the controller connects; nothing is saved on the controller.",
        f"effect = {_quote(settings.led.effect)}",
        '# Colors as "#rrggbb", up to 10. Static, pulse, strobe and wave use the first one; press_flash uses',
        "# up to 4, one per press.",
        f"colors = {_toml_value(_led_value(settings.led, 'colors'))}",
        "# Brightness 0-100; speed 1 (slow) to 10 (fast).",
        f"brightness = {settings.led.brightness}",
        f"speed = {settings.led.speed}",
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
        if settings.target(name) is not None:
            lines.append(f"{name} = {_quote(settings.target(name))}")
    lines += [
        "",
        "# Per-game profiles: settings that switch automatically while a game runs. A profile only lists",
        "# what's different from the settings above; everything else follows them. For example:",
        "#",
        "#   [[profile]]",
        '#   name = "Street Fighter 6"',
        '#   steam_app_id = 1364780      # a Steam game; for other games use process = "game.exe"',
        "#   [profile.gyro]",
        "#   sensitivity = 20.0",
        "#   [profile.remap]",
        '#   M1 = "M1"                   # M1 sends itself in this game, whatever [remap] says',
        "#   [profile.led]",
        '#   effect = "static"',
        '#   colors = ["#ff0000"]',
    ]
    for profile in settings.profiles:
        lines += ["", "[[profile]]", f"name = {_quote(profile.name)}"]
        if profile.steam_app_ids:
            lines.append(f"steam_app_id = {_toml_one_or_list(profile.steam_app_ids)}")
        if profile.processes:
            lines.append(f"process = {_toml_one_or_list(profile.processes)}")
        for section in SECTIONS:
            values = profile.overrides.get(section)
            if values:
                lines.append(f"[profile.{section}]")
                lines += [f"{key} = {_toml_value(value)}" for key, value in values.items()]
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
    return json.dumps(text)  # a valid TOML basic string for our button names, key combinations and names


def _toml_value(value) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    return _quote(value)


def _toml_one_or_list(values) -> str:
    return _toml_value(values[0]) if len(values) == 1 else "[" + ", ".join(_toml_value(v) for v in values) + "]"


def _table(data: dict, name: str, prefix: str = "") -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"'{prefix}{name}' should be a section like [{name}]")
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


def _integer(value, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} should be a whole number")
    if not low <= value <= high:
        raise ConfigError(f"{where} should be between {low} and {high}, not {value}")
    return value


def _colors(value, where: str) -> tuple[tuple[int, int, int], ...]:
    values = value if isinstance(value, list) else [value]
    if not 1 <= len(values) <= lighting.MAX_COLORS:
        raise ConfigError(f"{where} should list 1 to {lighting.MAX_COLORS} colors")
    colors = []
    for item in values:
        color = lighting.parse_color(item) if isinstance(item, str) else None
        if color is None:
            raise ConfigError(f'{where}: {item!r} isn\'t a color; write it like "#ff8800"')
        colors.append(color)
    return tuple(colors)


def _int_list(value, where: str) -> list[int]:
    values = value if isinstance(value, list) else [value]
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in values):
        raise ConfigError(f"{where} should be a Steam app ID number (or a list of them), like 1364780")
    return values


def _text_list(value, where: str) -> list[str]:
    values = value if isinstance(value, list) else [value]
    if not all(isinstance(v, str) and v.strip() for v in values):
        raise ConfigError(f'{where} should be a program name (or a list of them), like "game.exe"')
    return [v.strip() for v in values]


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
    profiles = [f"{profile.name} ({profile.describe_game()})" for profile in settings.profiles]
    return "\n".join([
        f"gyro: toggle {gyro.button}, ratchet {gyro.ratchet}, sensitivity {gyro.sensitivity:g} "
        f"(horizontal x{gyro.horizontal_scale:g}, vertical x{gyro.vertical_scale:g}), invert x {str(gyro.invert_x).lower()}, "
        f"invert y {str(gyro.invert_y).lower()}, tightening {gyro.tightening_dps:g}",
        f"sticks: left deadzone {settings.left_deadzone:g}, right deadzone {settings.right_deadzone:g}",
        "remap: " + (", ".join(sorted(remaps)) or "none"),
        "led: " + lighting.describe(settings.led),
        "profiles: " + ("; ".join(profiles) or "none"),
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
