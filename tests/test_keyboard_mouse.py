import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from evdev import ecodes as e  # noqa: E402

from vader5 import keyboard_mouse as km  # noqa: E402


class NameTests(unittest.TestCase):
    def test_key_names(self):
        self.assertEqual(km.key_code("space"), e.KEY_SPACE)
        self.assertEqual(km.key_code("A"), e.KEY_A)
        self.assertEqual(km.key_code("1"), e.KEY_1)
        self.assertEqual(km.key_code("F13"), e.KEY_F13)
        self.assertEqual(km.key_code(" ctrl "), e.KEY_LEFTCTRL)
        self.assertEqual(km.key_code("super"), e.KEY_LEFTMETA)
        self.assertEqual(km.key_code("printscreen"), e.KEY_SYSRQ)

    def test_unknown_or_non_keyboard_names(self):
        self.assertIsNone(km.key_code("banana"))
        self.assertIsNone(km.key_code("btn_left"))  # mouse buttons use mouse:, not key:
        self.assertIsNone(km.key_code(""))

    def test_mouse_buttons(self):
        self.assertEqual(km.mouse_code("left"), e.BTN_LEFT)
        self.assertEqual(km.mouse_code("Back"), e.BTN_SIDE)
        self.assertEqual(km.mouse_code("forward"), e.BTN_EXTRA)
        self.assertIsNone(km.mouse_code("wheel"))


class KeyChangeTests(unittest.TestCase):
    ctrl, c = e.KEY_LEFTCTRL, e.KEY_C

    def test_combination_presses_in_order_and_releases_in_reverse(self):
        self.assertEqual(km.key_changes((), (self.ctrl, self.c)), [(self.ctrl, 1), (self.c, 1)])
        self.assertEqual(km.key_changes((self.ctrl, self.c), ()), [(self.c, 0), (self.ctrl, 0)])

    def test_only_differences_are_sent(self):
        self.assertEqual(km.key_changes((self.ctrl, self.c), (self.ctrl,)), [(self.c, 0)])
        self.assertEqual(km.key_changes((self.ctrl,), (self.ctrl, self.c)), [(self.c, 1)])
        self.assertEqual(km.key_changes((self.ctrl,), (self.ctrl,)), [])


if __name__ == "__main__":
    unittest.main()
