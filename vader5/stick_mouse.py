"""Move the mouse pointer with a stick while a button is held.

Handy for menus, maps and launchers in games that expect a mouse. While the button is held the stick
only moves the pointer: it's kept out of the game, so you don't walk or swing the camera at the same
time. Mouse buttons come from the usual remaps ([remap] M1 = "mouse:left", for example).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import protocol

NONE = "NONE"
STICKS = {"left": "Left stick", "right": "Right stick"}
MAX_BATCH_S = 0.05  # a longer gap between reports can't cause a jump
NOMINAL_REPORT_S = 1 / 490
FULL = 32767.0


@dataclass
class StickMouseSettings:
    button: str = NONE  # hold this button to point with the stick; NONE = off
    stick: str = "right"
    speed: float = 900.0  # pixels a second at full tilt
    deadzone: float = 0.15  # ignore this much of the stick's travel around the center
    curve: float = 1.5  # 1 = straight, higher = finer control near the center


class StickMouse:
    """Stick-to-pointer movement without any devices, so it can be tested on its own."""

    def __init__(self, settings: StickMouseSettings | None = None):
        self.settings = settings or StickMouseSettings()
        self.active = False  # the button is held right now
        self._last_time: float | None = None
        self._remainder = [0.0, 0.0]  # sub-pixel movement carried to the next batch

    def process(self, states: list[protocol.InputState], now: float) -> tuple[int, int]:
        """Handle a batch of reports that arrived by `now`. Returns (mouse dx, mouse dy)."""
        if not states or self.settings.button == NONE or self.settings.stick not in STICKS:
            self.active = False
            self._last_time = now
            return 0, 0
        if self._last_time is None:
            elapsed = NOMINAL_REPORT_S * len(states)
        else:
            elapsed = min(now - self._last_time, MAX_BATCH_S)
        self._last_time = now
        dt = elapsed / len(states)

        was_active = self.active
        self.active = self.settings.button in states[-1].buttons
        if not self.active:
            self._remainder = [0.0, 0.0]
            return 0, 0
        if not was_active:
            self._remainder = [0.0, 0.0]  # start from a whole pixel each time it's pressed

        for state in states:
            if self.settings.button not in state.buttons:
                continue
            x, y = state.left_stick if self.settings.stick == "left" else state.right_stick
            speed_x, speed_y = self._speed(x / FULL, y / FULL)
            self._remainder[0] += speed_x * dt
            self._remainder[1] -= speed_y * dt  # the stick's +Y is up; the mouse's +Y is down
        dx, dy = int(self._remainder[0]), int(self._remainder[1])
        self._remainder[0] -= dx
        self._remainder[1] -= dy
        return dx, dy

    def suppressed_stick(self) -> str | None:
        """The stick that games shouldn't see right now, because it's moving the pointer."""
        return self.settings.stick if self.active else None

    def _speed(self, x: float, y: float) -> tuple[float, float]:
        """Pointer speed in pixels a second for a stick position, after the deadzone and curve."""
        distance = min(1.0, math.hypot(x, y))
        deadzone = self.settings.deadzone
        if distance <= deadzone:
            return 0.0, 0.0
        tilt = (distance - deadzone) / (1 - deadzone) if deadzone < 1 else 0.0
        speed = self.settings.speed * tilt ** max(1.0, self.settings.curve)
        return speed * x / distance, speed * y / distance
