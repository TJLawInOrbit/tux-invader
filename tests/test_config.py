import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from evdev import ecodes as e  # noqa: E402

from test_protocol import input_report  # noqa: E402
from vader5 import config, protocol  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_starter_file_gives_the_defaults(self):
        self.assertEqual(config.parse(config.STARTER), config.Settings())

    def test_empty_file_gives_the_defaults(self):
        self.assertEqual(config.parse(""), config.Settings())

    def test_values_are_read(self):
        settings = config.parse("""
            [gyro]
            button = "fn"
            sensitivity = 20
            invert_y = true
            tightening = 0
            horizontal_scale = 1.25
            [sticks]
            left_deadzone = 0.1
            [remap]
            M1 = "a"
            m2 = "none"
            menu = "guide"
        """)
        self.assertEqual(settings.gyro.button, "FN")
        self.assertEqual(settings.gyro.sensitivity, 20.0)
        self.assertTrue(settings.gyro.invert_y)
        self.assertFalse(settings.gyro.invert_x)
        self.assertEqual(settings.gyro.tightening_dps, 0.0)
        self.assertEqual((settings.gyro.horizontal_scale, settings.gyro.vertical_scale), (1.25, 1.0))
        self.assertEqual((settings.left_deadzone, settings.right_deadzone), (0.1, 0.0))
        self.assertEqual(settings.remap, {"M1": "A", "M2": config.NONE, "START": "HOME"})

    def test_mistakes_are_explained(self):
        cases = {
            '[gyro]\nsensitivity = "fast"': "sensitivity should be a number",
            "[gyro]\nsensitivity = true": "sensitivity should be a number",
            "[gyro]\nsensitivity = -1": "between 0 and 1000",
            "[gyro]\nspeed = 3": "unknown setting 'speed'",
            '[gyro]\nbutton = "BANANA"': "unknown button 'BANANA'",
            "[gyro]\ninvert_x = 1": "true or false",
            "[sticks]\nleft_deadzone = 1.5": "left_deadzone should be between",
            '[remap]\nM1 = "LT"': "unknown button 'LT'",  # triggers are analog, not a button to send
            '[remap]\nQ = "A"': "unknown button 'Q'",
            '[paddles]\nM1 = "A"': "unknown setting 'paddles'",
            "[gyro\nbutton = 1": "isn't valid TOML",
            "[gyro]\nhorizontal_scale = 20": "horizontal_scale should be between 0 and 10",
            '[gyro]\nvertical_scale = "more"': "vertical_scale should be a number",
            '[gyro]\nratchet = "turbo"': "can't be TURBO",
            '[gyro]\nbutton = "M3"\nratchet = "M3"': "can't be the same button",
            '[gyro]\nratchet = "PADDLE"': "unknown button 'PADDLE'",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(config.ConfigError, message):
                    config.parse(text)

    def test_key_and_mouse_remaps(self):
        settings = config.parse("""
            [remap]
            M1 = "key:space"
            M2 = "key:ctrl+c"
            C = "mouse:right"
            Z = "key:shift + mouse:left"
        """)
        self.assertEqual(settings.remap, {})
        codes = {name: combo.codes for name, combo in settings.key_remap.items()}
        self.assertEqual(codes, {
            "M1": (e.KEY_SPACE,),
            "M2": (e.KEY_LEFTCTRL, e.KEY_C),
            "C": (e.BTN_RIGHT,),
            "Z": (e.KEY_LEFTSHIFT, e.BTN_LEFT),
        })
        self.assertEqual(settings.key_remap["M2"].text, "key:ctrl+c")

    def test_key_remap_mistakes_are_explained(self):
        cases = {
            '[remap]\nM1 = "key:banana"': "unknown key 'banana'",
            '[remap]\nM1 = "keys:space"': "should start with key: or mouse:",
            '[remap]\nM1 = "mouse:wheel"': "unknown mouse button 'wheel'",
            '[remap]\nM1 = "key:"': "missing a key name",
            '[remap]\nM1 = "key:ctrl+"': "missing a key name",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(config.ConfigError, message):
                    config.parse(text)

    def test_missing_file_gives_the_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(config.load(os.path.join(folder, "none.toml")), config.Settings())


class ForGamesTests(unittest.TestCase):
    def test_remap_and_gyro_button(self):
        settings = config.parse('[remap]\nM1 = "A"\nM2 = "NONE"')
        self.assertEqual(settings.output_buttons(frozenset({"M1", "M2", "TURBO", "B"})), {"A", "B"})

    def test_keys_go_to_the_keyboard_not_the_controller(self):
        settings = config.parse('[remap]\nM2 = "key:ctrl+c"\nM1 = "key:space"\nTURBO = "key:a"')
        pressed = frozenset({"M2", "M1", "B", "TURBO"})
        self.assertEqual(settings.output_buttons(pressed), {"B"})
        # fixed button order (M1 before M2), no repeats, and the gyro toggle (TURBO) never presses keys
        self.assertEqual(settings.keys_for(pressed), (e.KEY_SPACE, e.KEY_LEFTCTRL, e.KEY_C))
        self.assertEqual(settings.keys_for(frozenset({"B"})), ())

    def test_ratchet_button_is_not_sent_anywhere(self):
        settings = config.parse('[gyro]\nratchet = "m2"\n[remap]\nM2 = "key:space"')
        self.assertEqual(settings.gyro.ratchet, "M2")
        pressed = frozenset({"M2", "A"})
        self.assertEqual(settings.output_buttons(pressed), {"A"})
        self.assertEqual(settings.keys_for(pressed), ())

    def test_deadzone(self):
        self.assertEqual(config.apply_deadzone(1000, 0, 0.1), (0, 0))
        self.assertEqual(config.apply_deadzone(32767, 0, 0.1), (32767, 0))
        x, y = config.apply_deadzone(16384, 0, 0.2)  # half travel -> (0.5 - 0.2) / 0.8 of full
        self.assertAlmostEqual(x / 32767, 0.375, delta=0.001)
        self.assertEqual(y, 0)
        self.assertEqual(config.apply_deadzone(-32768, -32768, 0.1), (-23170, -23170))  # diagonal stays in range
        self.assertEqual(config.apply_deadzone(1234, -5, 0.0), (1234, -5))

    def test_for_games_changes_buttons_and_sticks_only(self):
        settings = config.parse('[sticks]\nleft_deadzone = 0.2\n[remap]\nM1 = "A"')
        raw = protocol.parse_input(input_report(sticks=(1000, 0, 500, 0), bytes={13: 0x04, 15: 99}, gyro=(10, 20, 30)))
        shaped = settings.for_games(raw)
        self.assertEqual(shaped.buttons, {"A"})
        self.assertEqual(shaped.left_stick, (0, 0))
        self.assertEqual(shaped.right_stick, (500, 0))
        self.assertEqual((shaped.left_trigger, shaped.gyro), (99, raw.gyro))


class WatcherTests(unittest.TestCase):
    def test_reloads_on_save_and_keeps_last_good_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.toml")
            messages = []
            watcher = config.ConfigWatcher(path, log=messages.append)

            self.assertTrue(watcher.check(0.0))  # no file yet: defaults
            self.assertIn("no settings file", messages[-1])

            with open(path, "w") as f:
                f.write("[gyro]\nsensitivity = 30\n")
            self.assertFalse(watcher.check(0.5))  # only looks once a second
            self.assertTrue(watcher.check(1.1))
            self.assertEqual(watcher.settings.gyro.sensitivity, 30.0)

            with open(path, "w") as f:
                f.write("[gyro]\nsensitivity = 'very fast'\n")
            self.assertFalse(watcher.check(2.2))
            self.assertEqual(watcher.settings.gyro.sensitivity, 30.0)
            self.assertIn("mistake", messages[-1])

            self.assertFalse(watcher.check(3.3))  # unchanged file: nothing to do


if __name__ == "__main__":
    unittest.main()
