"""Gyro aiming: turn the controller's rotation into mouse movement.

A button toggles it (Turbo by default). Turning the controller left/right moves the mouse sideways,
tilting it up/down moves it vertically. Games see an ordinary mouse, so it works in any game that
accepts mouse aiming while you play with a controller, including through Steam.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import protocol


@dataclass
class GyroSettings:
    button: str = "TURBO"  # toggles gyro aiming on/off and isn't passed to games; Turbo sends one short pulse per press
    sensitivity: float = 15.0  # mouse movement (counts) per degree the controller turns
    invert_x: bool = False
    invert_y: bool = False
    tightening_dps: float = 1.0  # rotation slower than this (hand tremor) is scaled down


STILL_DPS = 1.5  # every axis slower than this, after calibration, counts as "held still"...
STILL_ACCEL_G = 0.1  # ...as long as gravity is steady to within this
SETTLE_S = 0.5  # held still this long before calibration starts
BIAS_TIME_CONSTANT_S = 1.0  # how quickly calibration follows the drift while held still
MAX_BATCH_S = 0.05  # a longer gap between reports (e.g. after a pause) can't cause a big jump
NOMINAL_REPORT_S = 1 / 490


class GyroAim:
    """Gyro processing without any devices, so it can be tested on its own."""

    def __init__(self, settings: GyroSettings | None = None):
        self.settings = settings or GyroSettings()
        self.enabled = False
        self.bias = [0.0, 0.0, 0.0]  # gyro reading (degrees/s) while the controller is still
        self._button_down = False
        self._still_s = 0.0
        self._last_time: float | None = None
        self._remainder = [0.0, 0.0]  # sub-count movement carried to the next batch

    def process(self, states: list[protocol.InputState], now: float) -> tuple[int, int, bool]:
        """Handle a batch of reports that arrived by `now`. Returns (mouse dx, mouse dy, toggled)."""
        if not states:
            return 0, 0, False
        if self._last_time is None:
            elapsed = NOMINAL_REPORT_S * len(states)
        else:
            elapsed = min(now - self._last_time, MAX_BATCH_S)
        self._last_time = now
        dt = elapsed / len(states)

        toggled = False
        turn_x = turn_y = 0.0  # degrees
        for state in states:
            pressed = self.settings.button in state.buttons
            if pressed and not self._button_down:
                self.enabled = not self.enabled
                self._remainder = [0.0, 0.0]
                toggled = True
            self._button_down = pressed

            pitch, _roll, yaw = self._calibrated_rates(state, dt)
            if self.enabled:
                turn_x -= self._tighten(yaw) * dt
                turn_y -= self._tighten(pitch) * dt

        if not self.enabled:
            return 0, 0, toggled
        s = self.settings
        self._remainder[0] += turn_x * s.sensitivity * (-1 if s.invert_x else 1)
        self._remainder[1] += turn_y * s.sensitivity * (-1 if s.invert_y else 1)
        dx, dy = int(self._remainder[0]), int(self._remainder[1])
        self._remainder[0] -= dx
        self._remainder[1] -= dy
        return dx, dy, toggled

    def _calibrated_rates(self, state: protocol.InputState, dt: float) -> list[float]:
        """Gyro rates minus drift. While the controller is still, the drift estimate follows the reading."""
        rates = [value - bias for value, bias in zip(state.gyro_dps, self.bias)]
        gravity = math.sqrt(sum(a * a for a in state.accel_g))
        if max(abs(r) for r in rates) < STILL_DPS and abs(gravity - 1.0) < STILL_ACCEL_G:
            self._still_s += dt
            if self._still_s >= SETTLE_S:
                alpha = min(1.0, dt / BIAS_TIME_CONSTANT_S)
                self.bias = [bias + rate * alpha for bias, rate in zip(self.bias, rates)]
        else:
            self._still_s = 0.0
        return rates

    def _tighten(self, rate: float) -> float:
        threshold = self.settings.tightening_dps
        if threshold <= 0 or abs(rate) >= threshold:
            return rate
        return rate * abs(rate) / threshold
