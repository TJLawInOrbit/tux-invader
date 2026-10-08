"""Gyro aiming: turn the controller's rotation into mouse movement.

A button toggles it (Turbo by default). Turning the controller left/right moves the mouse sideways,
tilting it up/down moves it vertically. Games see an ordinary mouse, so it works in any game that
accepts mouse aiming while you play with a controller, including through Steam.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import protocol

MODES = {
    "toggle": "Press the button to switch gyro aiming on, press again for off",
    "hold": "Gyro aiming is on only while the button is held",
}

# how left/right turning is measured
SPACES = {
    "controller": "Around the controller's own axis",  # holding it tilted turns slower
    "player": "Around the room's up direction",  # "player space": from gravity, so any grip angle works
}


@dataclass
class GyroSettings:
    button: str = "TURBO"  # switches gyro aiming on/off and isn't passed to games; Turbo sends one short pulse per press
    mode: str = "toggle"  # see MODES: press to switch, or aim only while the button is held
    ratchet: str = "NONE"  # hold to pause gyro aiming while you bring your hands back to center; not passed to games
    space: str = "controller"  # see SPACES
    sensitivity: float = 15.0  # mouse movement (counts) per degree the controller turns
    horizontal_scale: float = 1.0  # extra multiplier for left/right movement
    vertical_scale: float = 1.0  # extra multiplier for up/down movement
    invert_x: bool = False
    invert_y: bool = False
    tightening_dps: float = 1.0  # rotation slower than this (hand tremor) is scaled down


# Drift calibration only runs while the controller is set down. Measured on a controller lying on a desk:
# the gyro wobbles 0.05-0.14 deg/s and gravity 0.001-0.004 g. Held in hands it wobbles more, so slow
# aiming isn't mistaken for drift (which is small: up to about 0.1 deg/s).
REST_TIME_CONSTANT_S = 0.5  # how quickly the wobble measurement follows the readings
REST_GYRO_SD_DPS = 0.25  # every gyro axis wobbles less than this...
REST_ACCEL_SD_G = 0.006  # ...and gravity less than this...
REST_TURN_DPS = 1.0  # ...and it isn't turning steadily (on average) faster than this
SETTLE_S = 1.0  # resting this long before calibration starts
BIAS_TIME_CONSTANT_S = 2.0  # how quickly calibration follows the drift while resting
MAX_BATCH_S = 0.05  # a longer gap between reports (e.g. after a pause) can't cause a big jump
NOMINAL_REPORT_S = 1 / 490
GRAVITY_CORRECTION_S = 0.5  # player space: how quickly the up direction follows the accelerometer
PLAYER_YAW_RELAX = 1.41  # player space: lets a tilted grip still turn at full speed


class GyroAim:
    """Gyro processing without any devices, so it can be tested on its own."""

    def __init__(self, settings: GyroSettings | None = None):
        self.settings = settings or GyroSettings()
        self.enabled = False
        self.bias = [0.0, 0.0, 0.0]  # gyro reading (degrees/s) while the controller is still
        self._button_down = False
        self._paused = False
        self._rest_s = 0.0
        self._gyro_mean = [0.0, 0.0, 0.0]
        self._gyro_var = [(2 * REST_GYRO_SD_DPS) ** 2] * 3  # start out "not resting"
        self._accel_mean: list[float] | None = None
        self._accel_var = [(2 * REST_ACCEL_SD_G) ** 2] * 3
        self._up: tuple[float, float, float] | None = None  # up direction, in the controller's axes
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
            if self.settings.mode == "hold":
                if pressed != self.enabled:  # aiming only while the button is held
                    self.enabled = pressed
                    self._remainder = [0.0, 0.0]
                    toggled = True
            elif pressed and not self._button_down:
                self.enabled = not self.enabled
                self._remainder = [0.0, 0.0]
                toggled = True
            self._button_down = pressed

            paused = self.settings.ratchet in state.buttons
            if paused != self._paused:
                self._remainder = [0.0, 0.0]  # don't carry part of a count across a pause
                self._paused = paused

            rates = self._calibrated_rates(state, dt)  # keeps calibrating while paused
            self._track_gravity(rates, state.accel_g, dt)
            if self.enabled and not paused:
                turn_x -= self._tighten(self._yaw(rates)) * dt
                turn_y -= self._tighten(rates[0]) * dt

        if not self.enabled:
            return 0, 0, toggled
        s = self.settings
        self._remainder[0] += turn_x * s.sensitivity * s.horizontal_scale * (-1 if s.invert_x else 1)
        self._remainder[1] += turn_y * s.sensitivity * s.vertical_scale * (-1 if s.invert_y else 1)
        dx, dy = int(self._remainder[0]), int(self._remainder[1])
        self._remainder[0] -= dx
        self._remainder[1] -= dy
        return dx, dy, toggled

    def _calibrated_rates(self, state: protocol.InputState, dt: float) -> list[float]:
        """Gyro rates (pitch, roll, yaw) minus drift. While the controller rests, the drift estimate
        follows the reading."""
        raw, accel = state.gyro_dps, state.accel_g
        alpha = min(1.0, dt / REST_TIME_CONSTANT_S)
        if self._accel_mean is None:
            self._accel_mean = list(accel)
        for values, means, variances in ((raw, self._gyro_mean, self._gyro_var),
                                         (accel, self._accel_mean, self._accel_var)):
            for axis, value in enumerate(values):
                difference = value - means[axis]
                means[axis] += alpha * difference
                variances[axis] += alpha * (difference * difference - variances[axis])

        gravity = math.sqrt(sum(a * a for a in accel))
        resting = (max(self._gyro_var) < REST_GYRO_SD_DPS ** 2
                   and max(self._accel_var) < REST_ACCEL_SD_G ** 2
                   and max(abs(mean - bias) for mean, bias in zip(self._gyro_mean, self.bias)) < REST_TURN_DPS
                   and abs(gravity - 1.0) < 0.1)
        if resting:
            self._rest_s += dt
            if self._rest_s >= SETTLE_S:
                follow = min(1.0, dt / BIAS_TIME_CONSTANT_S)
                self.bias = [bias + (value - bias) * follow for bias, value in zip(self.bias, raw)]
        else:
            self._rest_s = 0.0
        return [value - bias for value, bias in zip(raw, self.bias)]

    def _track_gravity(self, rates: list[float], accel: tuple[float, float, float], dt: float) -> None:
        """Keep track of which way is up: turned along with the gyro, pulled slowly toward the accelerometer."""
        gravity = math.sqrt(sum(a * a for a in accel))
        if self._up is None:
            if gravity > 0.5:
                self._up = tuple(a / gravity for a in accel)
            return
        wx, wy, wz = (math.radians(rate) for rate in rates)
        ux, uy, uz = self._up
        # a direction that stays put in the room turns the other way as seen by the controller: du/dt = u x w
        ux, uy, uz = ux + (uy * wz - uz * wy) * dt, uy + (uz * wx - ux * wz) * dt, uz + (ux * wy - uy * wx) * dt
        if 0.7 < gravity < 1.3:  # not while shaking hard
            pull = min(1.0, dt / GRAVITY_CORRECTION_S)
            ux, uy, uz = (u + (a / gravity - u) * pull for u, a in zip((ux, uy, uz), accel))
        length = math.sqrt(ux * ux + uy * uy + uz * uz) or 1.0
        self._up = (ux / length, uy / length, uz / length)

    def _yaw(self, rates: list[float]) -> float:
        """Left/right turning rate, in the chosen space."""
        _pitch, roll, yaw = rates
        if self.settings.space != "player" or self._up is None:
            return yaw
        _, up_roll, up_yaw = self._up
        # turning around the room's up direction, from the controller's yaw and roll axes (pitch is up/down aim)
        around_up = roll * up_roll + yaw * up_yaw
        return math.copysign(min(abs(around_up) * PLAYER_YAW_RELAX, math.hypot(roll, yaw)), around_up)

    def _tighten(self, rate: float) -> float:
        threshold = self.settings.tightening_dps
        if threshold <= 0 or abs(rate) >= threshold:
            return rate
        return rate * abs(rate) / threshold
