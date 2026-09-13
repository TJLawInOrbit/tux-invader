"""A virtual keyboard and mouse: gyro aiming moves it, and buttons remapped to keys press its keys.

Games and the desktop see an ordinary keyboard and mouse ("Vader 5 Pro Keyboard and Mouse").
"""

from __future__ import annotations

from evdev import UInput
from evdev import ecodes as e

# Every standard keyboard key: codes 1-248 cover letters, numbers, F1-F24, media keys and more.
KEY_CODES = tuple(code for code in range(1, 249) if code in e.KEY)
MOUSE_BUTTONS = {
    "LEFT": e.BTN_LEFT, "RIGHT": e.BTN_RIGHT, "MIDDLE": e.BTN_MIDDLE,
    "BACK": e.BTN_SIDE, "SIDE": e.BTN_SIDE, "FORWARD": e.BTN_EXTRA, "EXTRA": e.BTN_EXTRA,
}
KEY_ALIASES = {
    "CTRL": "LEFTCTRL", "SHIFT": "LEFTSHIFT", "ALT": "LEFTALT",
    "SUPER": "LEFTMETA", "WIN": "LEFTMETA", "META": "LEFTMETA",
    "ESCAPE": "ESC", "RETURN": "ENTER", "DEL": "DELETE", "INS": "INSERT",
    "PGUP": "PAGEUP", "PGDN": "PAGEDOWN", "PRINTSCREEN": "SYSRQ", "BACKTICK": "GRAVE",
}


def key_code(name: str) -> int | None:
    """Code for a key name like "space", "a", "f13" or "ctrl"; None if there's no such key."""
    upper = name.strip().upper()
    code = e.ecodes.get("KEY_" + KEY_ALIASES.get(upper, upper))
    return code if code in KEY_CODES else None


def mouse_code(name: str) -> int | None:
    """Code for a mouse button name like "left" or "back"; None if there's no such button."""
    return MOUSE_BUTTONS.get(name.strip().upper())


def key_changes(held: tuple[int, ...], wanted: tuple[int, ...]) -> list[tuple[int, int]]:
    """Events (code, 1 = press / 0 = release) that turn `held` into `wanted`.

    Releases come first, newest first, then presses in the wanted order: ctrl+c presses ctrl
    before c and lets go of c before ctrl.
    """
    events = [(code, 0) for code in reversed(held) if code not in wanted]
    events += [(code, 1) for code in wanted if code not in held]
    return events


class VirtualKeyboardMouse:
    def __init__(self):
        capabilities = {
            e.EV_REL: [e.REL_X, e.REL_Y],
            e.EV_KEY: sorted(set(KEY_CODES) | set(MOUSE_BUTTONS.values())),
        }
        self._ui = UInput(capabilities, name="Vader 5 Pro Keyboard and Mouse", phys="vader5-pad/input1")
        self._held: tuple[int, ...] = ()

    @property
    def device_path(self) -> str:
        return self._ui.device.path

    def move(self, dx: int, dy: int) -> None:
        if dx:
            self._ui.write(e.EV_REL, e.REL_X, dx)
        if dy:
            self._ui.write(e.EV_REL, e.REL_Y, dy)
        if dx or dy:
            self._ui.syn()

    def hold(self, codes: tuple[int, ...]) -> None:
        """Keep exactly these keys and mouse buttons pressed."""
        events = key_changes(self._held, codes)
        for code, value in events:
            self._ui.write(e.EV_KEY, code, value)
            self._ui.syn()  # one report per key, so combinations arrive in order
        if events:
            self._held = tuple(c for c in self._held if c in codes) + tuple(c for c in codes if c not in self._held)

    def close(self) -> None:
        try:
            self.hold(())  # never leave a key stuck down
        except OSError:
            pass
        self._ui.close()
