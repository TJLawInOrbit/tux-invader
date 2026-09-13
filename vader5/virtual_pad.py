"""A virtual Xbox Elite Series 2 controller (uinput) driven by the Vader 5 Pro.

Games and Steam see a normal Xbox layout plus four back paddles:
- SDL (used by Steam and most Linux games) maps BTN_GRIPL/GRIPR/GRIPL2/GRIPR2 to paddles.
- Microsoft's vendor/product IDs make Steam treat it as an Xbox Elite controller.
Rumble requests from games arrive on the same device and are exposed as `rumble`.
"""

from __future__ import annotations

from evdev import AbsInfo, UInput
from evdev import ecodes as e

from . import protocol

NAME = "Microsoft X-Box One Elite 2 pad"  # what the kernel's xpad driver calls a real one
VENDOR_ID = 0x045E
PRODUCT_ID = 0x0B00

# Vader 5 Pro button -> evdev key code. LT/RT press bits aren't sent; the analog triggers are.
BUTTON_CODES = {
    "A": e.BTN_A, "B": e.BTN_B, "X": e.BTN_X, "Y": e.BTN_Y,
    "LB": e.BTN_TL, "RB": e.BTN_TR, "L3": e.BTN_THUMBL, "R3": e.BTN_THUMBR,
    "SELECT": e.BTN_SELECT, "START": e.BTN_START, "HOME": e.BTN_MODE,
    "UP": e.BTN_DPAD_UP, "DOWN": e.BTN_DPAD_DOWN, "LEFT": e.BTN_DPAD_LEFT, "RIGHT": e.BTN_DPAD_RIGHT,
    # Back buttons -> Elite paddles. SDL: GRIPL/GRIPR = upper left/right, GRIPL2/GRIPR2 = lower.
    "M1": e.BTN_GRIPL, "M2": e.BTN_GRIPR, "M3": e.BTN_GRIPL2, "M4": e.BTN_GRIPR2,
    # No Xbox equivalent: extra joystick buttons that Steam and some games can still bind.
    "C": e.BTN_TRIGGER_HAPPY1, "Z": e.BTN_TRIGGER_HAPPY2,
    "LM": e.BTN_TRIGGER_HAPPY3, "RM": e.BTN_TRIGGER_HAPPY4,
    "FN": e.BTN_TRIGGER_HAPPY5, "TURBO": e.BTN_TRIGGER_HAPPY6,
}

STICK = AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)
TRIGGER = AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)
AXES = (
    (e.ABS_X, STICK), (e.ABS_Y, STICK), (e.ABS_RX, STICK), (e.ABS_RY, STICK),
    (e.ABS_Z, TRIGGER), (e.ABS_RZ, TRIGGER),
)


def evdev_values(state: protocol.InputState, hidden: frozenset[str] = frozenset()) -> dict[tuple[int, int], int]:
    """Every (event type, code) -> value the virtual controller should report for this state.

    Buttons named in `hidden` (e.g. the gyro toggle) are always reported as released.
    """
    lx, ly = state.left_stick
    rx, ry = state.right_stick
    values = {
        # evdev's +Y is down; ~ flips the sign without overflowing at -32768 (same as xpad).
        (e.EV_ABS, e.ABS_X): lx, (e.EV_ABS, e.ABS_Y): ~ly,
        (e.EV_ABS, e.ABS_RX): rx, (e.EV_ABS, e.ABS_RY): ~ry,
        (e.EV_ABS, e.ABS_Z): state.left_trigger, (e.EV_ABS, e.ABS_RZ): state.right_trigger,
    }
    for name, code in BUTTON_CODES.items():
        values[(e.EV_KEY, code)] = int(name in state.buttons and name not in hidden)
    return values


def neutral_values() -> dict[tuple[int, int], int]:
    """Sticks centered, triggers and buttons released."""
    values = {(e.EV_ABS, code): 0 for code, _ in AXES}
    values.update({(e.EV_KEY, code): 0 for code in BUTTON_CODES.values()})
    return values


class VirtualElite:
    def __init__(self):
        capabilities = {
            e.EV_KEY: sorted(set(BUTTON_CODES.values())),
            e.EV_ABS: list(AXES),
            e.EV_FF: [e.FF_RUMBLE],
        }
        self._ui = UInput(
            capabilities, name=NAME, vendor=VENDOR_ID, product=PRODUCT_ID, version=1,
            bustype=e.BUS_USB, phys="vader5-pad/input0", max_effects=16,
        )
        self._reported = neutral_values()
        self._effects: dict[int, tuple[int, int, int]] = {}  # effect id -> (strong, weak, length ms)
        self._playing: int | None = None
        self._stop_at: float | None = None
        self.rumble: tuple[int, int] = (0, 0)  # motor levels (0-255) games currently ask for

    @property
    def device_path(self) -> str:
        return self._ui.device.path

    def fileno(self) -> int:
        return self._ui.fd

    def update(self, state: protocol.InputState, hidden: frozenset[str] = frozenset()) -> None:
        self._send(evdev_values(state, hidden))

    def release_all(self) -> None:
        self._send(neutral_values())

    def handle_events(self, now: float) -> None:
        """Process rumble uploads, plays and stops from games; updates `rumble`."""
        try:
            events = list(self._ui.read())
        except BlockingIOError:
            events = []
        for event in events:
            if event.type == e.EV_UINPUT and event.code == e.UI_FF_UPLOAD:
                self._upload(event.value)
            elif event.type == e.EV_UINPUT and event.code == e.UI_FF_ERASE:
                self._erase(event.value)
            elif event.type == e.EV_FF and event.code in self._effects:
                if event.value > 0:
                    self._play(event.code, now)
                elif event.code == self._playing:
                    self._stop()
        if self._stop_at is not None and now >= self._stop_at:
            self._stop()

    def close(self) -> None:
        self._ui.close()

    def _send(self, values: dict[tuple[int, int], int]) -> None:
        changed = False
        for key, value in values.items():
            if self._reported.get(key) != value:
                self._ui.write(key[0], key[1], value)
                self._reported[key] = value
                changed = True
        if changed:
            self._ui.syn()

    def _upload(self, request_id: int) -> None:
        upload = self._ui.begin_upload(request_id)
        effect = upload.effect
        rumble = effect.u.ff_rumble_effect  # the kernel only lets FF_RUMBLE through
        self._effects[effect.id] = (
            rumble.strong_magnitude >> 8, rumble.weak_magnitude >> 8, effect.ff_replay.length,
        )
        if effect.id == self._playing:  # games often update a playing effect in place
            self.rumble = self._effects[effect.id][:2]
        upload.retval = 0
        self._ui.end_upload(upload)

    def _erase(self, request_id: int) -> None:
        erase = self._ui.begin_erase(request_id)
        self._effects.pop(erase.effect_id, None)
        if erase.effect_id == self._playing:
            self._stop()
        erase.retval = 0
        self._ui.end_erase(erase)

    def _play(self, effect_id: int, now: float) -> None:
        strong, weak, length_ms = self._effects[effect_id]
        self._playing = effect_id
        self.rumble = (strong, weak)
        self._stop_at = now + length_ms / 1000 if length_ms else None

    def _stop(self) -> None:
        self._playing = None
        self._stop_at = None
        self.rumble = (0, 0)
