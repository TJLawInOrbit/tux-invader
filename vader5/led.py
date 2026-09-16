"""LED strip effects for the Vader 5 Pro.

The controller keeps its lights as a small "LED blob": a 20-byte header followed by animation frames,
each holding one colour per LED zone (10 zones x 10 frames on the Vader 5 Pro). vader5-pad sends it
with the live LED commands, so the lights change at once and nothing is saved to the controller's
memory: switching the controller off and on brings back its own lights.

Layout from flydigi-vader-pro-5-ctl's protocol notes (Part 4), checked against a real controller:
reading, a static colour and restoring the original were confirmed on the strip.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

HEADER_SIZE = 20
BLACK = (0, 0, 0)
DEFAULT_COLOR = (0, 128, 255)
MAX_COLORS = 10
# speed 1 (slow) .. 10 (fast). The controller stores a period, so a bigger number is slower;
# 4 is what its factory rainbow uses.
SPEED_LOOP_TIME = (30, 26, 22, 18, 15, 12, 9, 6, 4, 2)


@dataclass(frozen=True)
class Effect:
    name: str  # as written in the settings file
    label: str  # as shown in the settings window
    code: int  # the controller's effect number
    min_colors: int
    max_colors: int
    animated: bool  # uses the speed setting
    experimental: bool = False  # built from animation frames; not yet seen on a real controller
    hint: str = ""


EFFECTS: dict[str, Effect] = {effect.name: effect for effect in (
    Effect("controller", "Controller's own lights", 0, 0, 0, False,
           hint="The app leaves the LED strip alone, so it shows the lights stored on the controller."),
    Effect("off", "Off", 6, 0, 0, False, hint="The LED strip is switched off."),
    Effect("static", "Static color", 5, 1, 1, False, hint="One steady color."),
    Effect("zones", "Color per zone", 5, 1, MAX_COLORS, False, True,
           "Each of the LED zones gets the next color in the list, repeating if there are fewer colors."),
    Effect("breath", "Breathing", 2, 1, 5, True, hint="Fades in and out, through up to 5 colors."),
    Effect("pulse", "Pulse", 7, 1, 1, True, True, "A quick flash that fades out, over and over."),
    Effect("color_cycle", "Color cycle", 3, 2, MAX_COLORS, True, hint="Moves through 2 to 10 colors."),
    Effect("rainbow", "Rainbow", 7, 0, 0, True,
           hint="A rainbow moving along the strip, like the controller's factory lights."),
    Effect("strobe", "Strobe", 7, 1, 1, True, True, "Flashes a color on and off."),
    Effect("wave", "Wave", 7, 1, 1, True, True, "A color running along the strip."),
    # The controller's own press-feedback effect (4) only pulses by itself, so the app does this one:
    # the strip stays off (6) and PressFlash lights it while a button is held.
    Effect("press_flash", "Flash on button press", 6, 1, 4, False,
           hint="Lights up while you press a button and goes dark when you let go. With several colors, each press uses "
                "the next one (up to 4); with Specific buttons, only the buttons you choose flash, each in its own "
                "color. The app does this, so it only works while the background service is running."),
)}


@dataclass(frozen=True)
class LedSettings:
    effect: str = "controller"
    colors: tuple[tuple[int, int, int], ...] = (DEFAULT_COLOR,)
    brightness: int = 50  # 0-100
    speed: int = 5  # 1 (slow) - 10 (fast)
    specific_buttons: bool = False  # press_flash: only the buttons in button_colors flash, each in its own color
    button_colors: tuple[tuple[str, tuple[int, int, int]], ...] = ()  # (button name, color), in button order


def parse_color(text: str) -> tuple[int, int, int] | None:
    """'#rrggbb' (or 'rrggbb') to (r, g, b); None if it isn't a color."""
    value = text.strip().lstrip("#")
    if len(value) != 6:
        return None
    try:
        number = int(value, 16)
    except ValueError:
        return None
    return (number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF


def color_text(color: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color)


def geometry(blob: bytes) -> tuple[int, int]:
    """(LED zones, animation frames) of an LED blob read from the controller."""
    if len(blob) <= HEADER_SIZE or blob[7] == 0:
        raise ValueError("that isn't the controller's LED data")
    zones = blob[7]
    return zones, (len(blob) - HEADER_SIZE) // (zones * 3)


def describe(lights: LedSettings) -> str:
    effect = EFFECTS[lights.effect]
    parts = [effect.label]
    if lights.effect == "press_flash" and lights.specific_buttons:
        parts.append("specific buttons: " + (" ".join(f"{name} {color_text(color)}" for name, color in lights.button_colors)
                                              or "none chosen"))
    elif effect.max_colors:
        parts.append(" ".join(color_text(color) for color in lights.colors[:effect.max_colors]))
    if lights.effect not in ("controller", "off"):
        parts.append(f"brightness {lights.brightness}")
    if effect.animated:
        parts.append(f"speed {lights.speed}")
    return ", ".join(parts)


def _scaled(color: tuple[int, int, int], level: float) -> tuple[int, int, int]:
    return tuple(round(channel * level) for channel in color)


def _rainbow(zone: int, frame: int, frames: int) -> tuple[int, int, int]:
    # a third of the color wheel along the strip, moving one full turn over the animation (seamless loop)
    hue = (zone / 30 - frame / frames) % 1.0
    return tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, 1.0, 1.0))


def build_blob(lights: LedSettings, zones: int, frames: int) -> bytes:
    """The LED blob for these settings, for a controller with `zones` LEDs and `frames` animation frames."""
    effect = EFFECTS[lights.effect]
    if effect.name == "controller":
        raise ValueError("'controller' means the app doesn't send any LED settings")
    colors = list(lights.colors) or [DEFAULT_COLOR]
    first = colors[0]
    grid = [[BLACK] * zones for _ in range(frames)]
    loop_end = 0

    if effect.name == "static":
        grid[0] = [first] * zones
    elif effect.name == "zones":
        grid[0] = [colors[zone % len(colors)] for zone in range(zones)]
    elif effect.name == "breath":  # the controller fades between colors on even frames and black on odd ones
        used = colors[:max(1, frames // 2)]
        for index, color in enumerate(used):
            grid[2 * index] = [color] * zones
        loop_end = 2 * len(used) - 1
    elif effect.name == "color_cycle":
        used = colors[:frames]
        for index, color in enumerate(used):
            grid[index] = [color] * zones
        loop_end = len(used) - 1
    elif effect.name == "rainbow":
        grid = [[_rainbow(zone, frame, frames) for zone in range(zones)] for frame in range(frames)]
        loop_end = frames - 1
    elif effect.name == "pulse":
        for frame in range(frames):
            level = (frame + 1) / 3 if frame < 2 else max(0.0, 1 - (frame - 2) / max(1, frames - 3))
            grid[frame] = [_scaled(first, level)] * zones
        loop_end = frames - 1
    elif effect.name == "strobe":
        grid[0] = [first] * zones  # frame 1 stays black
        loop_end = 1
    elif effect.name == "wave":
        steps = min(frames, zones)
        for frame in range(steps):
            grid[frame][(frame - 1) % zones] = _scaled(first, 0.3)
            grid[frame][(frame + 1) % zones] = _scaled(first, 0.3)
            grid[frame][frame] = first
        loop_end = steps - 1
    # "off" and "press_flash" keep every frame black

    header = bytes([
        0, 3,  # blob version 3.0
        0,  # the controller's click-feedback flag; it doesn't react to presses (see PressFlash)
        0, loop_end,  # animation plays frames 0..loop_end
        # press_flash: the smallest value, in case the controller eases color changes with it
        FLASH_LOOP_TIME if effect.name == "press_flash" else SPEED_LOOP_TIME[lights.speed - 1],
        lights.brightness,
        zones,
        effect.code,
        0,  # grip lights don't follow along
    ])
    frames_data = bytes(channel for frame in grid for color in frame for channel in color)
    return header + b"\xff" * (HEADER_SIZE - len(header)) + frames_data


FLASH_LOOP_TIME = 1  # the controller's transition value for the dark background of press_flash
FLASH_MAX_COLORS = 4
FLASH_STEP_S = 0.015  # least time between colors sent (a press is always sent at once)
FLASH_FADE = (0.0,)  # brightness steps after the button is let go: straight to dark


class PressFlash:
    """"Flash on button press", done by the app with the instant-color command: the strip lights up while a
    button is held and goes dark when it's let go. Normally any button flashes, each new press in the next of
    up to 4 colors; with specific buttons, only the chosen buttons flash, each in its own color.
    (The controller's own press-feedback effect only pulses by itself, with or without test mode.)"""

    def __init__(self):
        self.colors: list[tuple[int, int, int]] = []  # colors taking turns, for any button
        self.button_colors: dict[str, tuple[int, int, int]] = {}  # or: button -> its own color
        self._index = -1  # color of the current or last press
        self._was_pressed = False
        self._held: list[str] = []  # held buttons that have a color, in the order they were pressed
        self._current: tuple[int, int, int] | None = None  # the color the strip shows or fades from
        self._fade_step = len(FLASH_FADE)
        self._sent: tuple[int, int, int] | None = None
        self._next_send = 0.0

    def configure(self, lights: LedSettings) -> None:
        """Call whenever the LED settings change (they reset the strip)."""
        self.colors, self.button_colors = [], {}
        if lights.effect == "press_flash":
            level = lights.brightness / 100
            if lights.specific_buttons:
                self.button_colors = {name: _scaled(color, level) for name, color in lights.button_colors}
            else:
                self.colors = [_scaled(color, level) for color in lights.colors[:FLASH_MAX_COLORS] or (DEFAULT_COLOR,)]
        self._index, self._was_pressed, self._held, self._current = -1, False, [], None
        self._fade_step, self._sent = len(FLASH_FADE), None

    def update(self, buttons: frozenset[str], now: float) -> tuple[int, int, int] | None:
        """The color to show right now for the buttons held, or None when nothing needs sending."""
        if not self.colors and not self.button_colors:
            return None
        wanted = self._color_for(buttons)
        if wanted is not None:
            self._current, self._fade_step = wanted, 0
        else:
            if self._current is None or now < self._next_send or self._fade_step >= len(FLASH_FADE):
                return None
            wanted = _scaled(self._current, FLASH_FADE[self._fade_step])
            self._fade_step += 1
        if wanted == self._sent:
            return None
        self._sent, self._next_send = wanted, now + FLASH_STEP_S
        return wanted

    def _color_for(self, buttons: frozenset[str]) -> tuple[int, int, int] | None:
        """The lit color for these held buttons, or None if none of them lights the strip."""
        if self.button_colors:  # the most recently pressed button with a color wins
            self._held = [name for name in self._held if name in buttons]
            self._held += sorted(name for name in buttons if name in self.button_colors and name not in self._held)
            return self.button_colors[self._held[-1]] if self._held else None
        pressed = bool(buttons)
        if pressed and not self._was_pressed:  # a new press: next color
            self._index = (self._index + 1) % len(self.colors)
        self._was_pressed = pressed
        return self.colors[self._index] if pressed else None
