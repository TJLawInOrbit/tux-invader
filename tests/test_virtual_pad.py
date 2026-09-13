import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from evdev import ecodes as e  # noqa: E402

from test_protocol import input_report  # noqa: E402
from vader5 import protocol, virtual_pad  # noqa: E402


def state(**fields):
    return protocol.parse_input(input_report(**fields))


class MappingTests(unittest.TestCase):
    def test_every_button_has_a_code(self):
        digital_triggers = {"LT", "RT"}  # the analog trigger axes are sent instead
        self.assertEqual(set(virtual_pad.BUTTON_CODES), set(protocol.BUTTON_NAMES) - digital_triggers)
        self.assertEqual(len(set(virtual_pad.BUTTON_CODES.values())), len(virtual_pad.BUTTON_CODES))

    def test_buttons_and_paddles(self):
        values = virtual_pad.evdev_values(state(bytes={11: 0x10 | 0x01, 13: 0x04 | 0x08 | 0x10 | 0x20}))
        pressed = {code for (etype, code), value in values.items() if etype == e.EV_KEY and value}
        self.assertEqual(pressed, {e.BTN_A, e.BTN_DPAD_UP, e.BTN_GRIPL, e.BTN_GRIPR, e.BTN_GRIPL2, e.BTN_GRIPR2})

    def test_back_buttons_are_steam_paddles_p1_to_p4(self):
        # SDL (and so Steam) numbers the Elite paddles GRIPR, GRIPL, GRIPR2, GRIPL2 as paddle 1-4
        self.assertEqual([virtual_pad.BUTTON_CODES[name] for name in ("M1", "M2", "M3", "M4")],
                         [e.BTN_GRIPR, e.BTN_GRIPL, e.BTN_GRIPR2, e.BTN_GRIPL2])

    def test_axes(self):
        values = virtual_pad.evdev_values(state(sticks=(1234, 100, -32768, -32768), bytes={15: 200, 16: 3}))
        self.assertEqual(values[(e.EV_ABS, e.ABS_X)], 1234)
        self.assertEqual(values[(e.EV_ABS, e.ABS_Y)], -101)  # up on the controller -> negative (up) in evdev
        self.assertEqual(values[(e.EV_ABS, e.ABS_RX)], -32768)
        self.assertEqual(values[(e.EV_ABS, e.ABS_RY)], 32767)  # no overflow at the extreme
        self.assertEqual((values[(e.EV_ABS, e.ABS_Z)], values[(e.EV_ABS, e.ABS_RZ)]), (200, 3))

    def test_neutral_covers_every_reported_value(self):
        self.assertEqual(set(virtual_pad.neutral_values()), set(virtual_pad.evdev_values(state())))


if __name__ == "__main__":
    unittest.main()
