import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from test_protocol import input_report  # noqa: E402
from vader5 import protocol  # noqa: E402
from vader5.stick_mouse import StickMouse, StickMouseSettings  # noqa: E402

FULL = 32767


def report(right=(0.0, 0.0), left=(0.0, 0.0), buttons=()):
    """An input state with the sticks at these positions (-1.0 to 1.0) and these buttons held."""
    button_bytes = {}
    for offset, mask, name in protocol.BUTTON_BITS:
        if name in buttons:
            button_bytes[offset] = button_bytes.get(offset, 0) | mask
    sticks = (round(left[0] * FULL), round(left[1] * FULL), round(right[0] * FULL), round(right[1] * FULL))
    return protocol.parse_input(input_report(sticks=sticks, bytes=button_bytes))


class StickMouseTests(unittest.TestCase):
    def feed(self, pointer, state, seconds, start=0.0, batch=10, rate=490.0):
        """Feed `seconds` of identical reports in batches, like the controller does."""
        now, dx, dy = start, 0, 0
        for _ in range(int(seconds * rate / batch)):
            now += batch / rate
            move_x, move_y = pointer.process([state] * batch, now)
            dx, dy = dx + move_x, dy + move_y
        return dx, dy, now

    def test_off_until_a_button_is_chosen(self):
        pointer = StickMouse()  # the default is "NONE": no pointer button
        dx, dy, _ = self.feed(pointer, report(right=(1.0, 0.0), buttons={"M3"}), 1.0)
        self.assertEqual((dx, dy), (0, 0))
        self.assertFalse(pointer.active)
        self.assertIsNone(pointer.suppressed_stick())

    def test_the_stick_moves_the_pointer_while_the_button_is_held(self):
        pointer = StickMouse(StickMouseSettings(button="M3", stick="right", speed=900, deadzone=0.0, curve=1.0))
        dx, dy, now = self.feed(pointer, report(right=(1.0, 0.0), buttons={"M3"}), 1.0)
        self.assertAlmostEqual(dx, 900, delta=25)  # 900 pixels a second at full tilt
        self.assertEqual(dy, 0)
        self.assertTrue(pointer.active)
        self.assertEqual(pointer.suppressed_stick(), "right")  # games don't see that stick meanwhile

        _, up, _ = self.feed(pointer, report(right=(0.0, 1.0), buttons={"M3"}), 1.0, start=now)
        self.assertLess(up, 0)  # pushing the stick up moves the pointer up the screen

    def test_nothing_moves_without_the_button(self):
        pointer = StickMouse(StickMouseSettings(button="M3", speed=900, deadzone=0.0))
        dx, dy, now = self.feed(pointer, report(right=(1.0, 0.0)), 1.0)
        self.assertEqual((dx, dy), (0, 0))
        self.assertFalse(pointer.active)
        self.assertIsNone(pointer.suppressed_stick())

    def test_the_other_stick_and_the_deadzone_are_ignored(self):
        pointer = StickMouse(StickMouseSettings(button="M3", stick="right", speed=900, deadzone=0.2, curve=1.0))
        still, _, now = self.feed(pointer, report(left=(1.0, 0.0), buttons={"M3"}), 1.0)
        self.assertEqual(still, 0)  # the left stick isn't the one that points
        small, _, now = self.feed(pointer, report(right=(0.1, 0.0), buttons={"M3"}), 1.0, start=now)
        self.assertEqual(small, 0)  # inside the deadzone
        half, _, _ = self.feed(pointer, report(right=(0.6, 0.0), buttons={"M3"}), 1.0, start=now)
        self.assertAlmostEqual(half, 900 * 0.5, delta=25)  # 0.6 is halfway past a 0.2 deadzone

    def test_the_curve_slows_small_movements(self):
        half = report(right=(0.5, 0.0), buttons={"M3"})
        straight = StickMouse(StickMouseSettings(button="M3", speed=900, deadzone=0.0, curve=1.0))
        curved = StickMouse(StickMouseSettings(button="M3", speed=900, deadzone=0.0, curve=2.0))
        plain, _, _ = self.feed(straight, half, 1.0)
        fine, _, _ = self.feed(curved, half, 1.0)
        self.assertAlmostEqual(plain, 450, delta=20)
        self.assertAlmostEqual(fine, 225, delta=20)  # half tilt, squared

    def test_a_pointer_stick_is_kept_from_games(self):
        from vader5 import config
        settings = config.parse('[pointer]\nbutton = "M3"\nstick = "right"')
        state = report(right=(1.0, 0.5), left=(0.4, 0.0), buttons={"M3"})
        playing = settings.for_games(state)
        self.assertNotEqual(playing.right_stick, (0, 0))  # not pointing: the game sees the stick
        pointing = settings.for_games(state, "right")
        self.assertEqual(pointing.right_stick, (0, 0))
        self.assertEqual(pointing.left_stick, state.left_stick)  # the other stick still plays
        self.assertNotIn("M3", pointing.buttons)  # the hold button itself never reaches games


if __name__ == "__main__":
    unittest.main()
