import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from evdev import ecodes as e  # noqa: E402

from test_protocol import input_report  # noqa: E402
from vader5 import config, led, protocol  # noqa: E402


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

    def test_gyro_space(self):
        self.assertEqual(config.parse("").gyro.space, "controller")
        self.assertEqual(config.parse('[gyro]\nspace = "Player"').gyro.space, "player")
        with self.assertRaisesRegex(config.ConfigError, "space must be"):
            config.parse('[gyro]\nspace = "world"')

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


class ProfileTests(unittest.TestCase):
    TEXT = """
        [gyro]
        sensitivity = 15
        ratchet = "M2"
        [remap]
        M1 = "key:space"

        [[profile]]
        name = "Street Fighter 6"
        steam_app_id = 1364780
        [profile.gyro]
        sensitivity = 20
        [profile.remap]
        m1 = "m1"
        C = "key:f"

        [[profile]]
        name = "Retro"
        process = ["RetroArch", "retroarch.exe"]
    """

    def test_profiles_change_some_settings_and_follow_the_rest(self):
        settings = config.parse(self.TEXT)
        sf6 = settings.for_profile("Street Fighter 6")
        self.assertEqual(sf6.gyro.sensitivity, 20.0)
        self.assertEqual(sf6.gyro.ratchet, "M2")  # follows the main settings
        self.assertEqual((sf6.target("M1"), sf6.target("C")), ("M1", "key:f"))
        self.assertEqual(sf6.output_buttons(frozenset({"M1"})), {"M1"})  # back to itself in this game
        self.assertEqual(settings.for_profile("Retro").gyro.sensitivity, 15.0)
        self.assertIs(settings.for_profile(None), settings)
        self.assertIs(settings.for_profile("No such game"), settings)
        self.assertEqual(settings.profiles[1].processes, ("retroarch", "retroarch.exe"))
        self.assertEqual(settings.profiles[0].overrides,
                         {"gyro": {"sensitivity": 20.0}, "remap": {"M1": "M1", "C": "key:f"}})
        self.assertIn("Street Fighter 6 (Steam game 1364780)", config.describe(settings))

    def test_profiles_survive_saving(self):
        settings = config.parse(self.TEXT)
        self.assertEqual(config.parse(config.render(settings)), settings)

    def test_profile_mistakes_are_explained(self):
        cases = {
            '[[profile]]\nsteam_app_id = 1': "needs a name",
            '[[profile]]\nname = "A"': 'profile "A" needs steam_app_id or process',
            '[[profile]]\nname = "A"\nsteam_app_id = "abc"': 'profile "A" steam_app_id should be a Steam app ID',
            '[[profile]]\nname = "A"\nprocess = 5': 'profile "A" process should be a program name',
            '[[profile]]\nname = "A"\nprocess = "a"\n[[profile]]\nname = "a"\nprocess = "b"': 'two profiles called "a"',
            '[[profile]]\nname = "A"\nprocess = "a"\n[profile.gyro]\nsensitivity = "fast"':
                'profile "A" [gyro] sensitivity should be a number',
            '[[profile]]\nname = "A"\nprocess = "a"\nspeed = 2': "unknown setting 'speed' in profile \"A\"",
            '[gyro]\nratchet = "M2"\n[[profile]]\nname = "A"\nprocess = "a"\n[profile.gyro]\nbutton = "M2"':
                'profile "A" [gyro] ratchet and button can\'t be the same button',
            '[profile]\nname = "A"': "should be written as [[profile]] sections",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError) as caught:
                    config.parse(text)
                self.assertIn(message, str(caught.exception))

    def test_overrides_between(self):
        main = config.parse('[gyro]\nsensitivity = 15\n[remap]\nM1 = "key:space"')
        effective = config.parse('[gyro]\nsensitivity = 22\ninvert_y = true\n[sticks]\nleft_deadzone = 0.1\n'
                                 '[remap]\nC = "NONE"')
        self.assertEqual(config.overrides_between(main, effective), {
            "gyro": {"sensitivity": 22.0, "invert_y": True},
            "sticks": {"left_deadzone": 0.1},
            "remap": {"M1": "M1", "C": "NONE"},
        })
        self.assertEqual(config.overrides_between(main, main), {})


class LedSettingsTests(unittest.TestCase):
    def test_led_section(self):
        settings = config.parse('[led]\neffect = "Breath"\ncolors = ["#FF0000", "0000ff"]\nbrightness = 80\nspeed = 7')
        self.assertEqual(settings.led, led.LedSettings("breath", ((255, 0, 0), (0, 0, 255)), 80, 7))
        self.assertEqual(config.parse(config.render(settings)), settings)
        self.assertEqual(config.Settings().led.effect, "controller")  # the app leaves the lights alone by default
        self.assertIn("led: Breathing, #ff0000 #0000ff, brightness 80, speed 7", config.describe(settings))

    def test_led_mistakes_are_explained(self):
        cases = {
            '[led]\neffect = "disco"': "unknown effect 'disco'",
            '[led]\ncolors = ["#12"]': "isn't a color",
            '[led]\ncolors = []': "1 to 10 colors",
            '[led]\nbrightness = 101': "brightness should be between 0 and 100",
            '[led]\nspeed = 0': "speed should be between 1 and 10",
            '[led]\nspeed = 2.5': "speed should be a whole number",
            '[led]\nglow = 1': "unknown setting 'glow' in [led]",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError) as caught:
                    config.parse(text)
                self.assertIn(message, str(caught.exception))

    def test_specific_button_colors(self):
        settings = config.parse('[led]\neffect = "press_flash"\nspecific_buttons = true\n'
                                'button_colors = { lb = "#FF0000", A = "00ff00", view = "#0000ff" }')
        self.assertTrue(settings.led.specific_buttons)
        self.assertEqual(dict(settings.led.button_colors), {"LB": (255, 0, 0), "A": (0, 255, 0), "SELECT": (0, 0, 255)})
        self.assertEqual(config.parse(config.render(settings)), settings)
        self.assertIn("specific buttons: A #00ff00", config.describe(settings))
        self.assertEqual(config.Settings().led.button_colors, ())
        cases = {
            '[led]\nbutton_colors = { Q = "#ff0000" }': "unknown button 'Q'",
            '[led]\nbutton_colors = { A = "red" }': "isn't a color",
            '[led]\nbutton_colors = ["#ff0000"]': "should list buttons and their colors",
            '[led]\nspecific_buttons = "yes"': "specific_buttons",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError) as caught:
                    config.parse(text)
                self.assertIn(message, str(caught.exception))
        profiled = config.parse('[led]\neffect = "press_flash"\n[[profile]]\nname = "SF6"\nsteam_app_id = 1364780\n'
                                '[profile.led]\nspecific_buttons = true\nbutton_colors = { A = "#00ff00" }')
        self.assertEqual(profiled.for_profile("SF6").led.button_colors, (("A", (0, 255, 0)),))
        self.assertEqual(config.parse(config.render(profiled)), profiled)

    def test_game_profiles_can_have_their_own_lights(self):
        settings = config.parse('[led]\neffect = "static"\ncolors = ["#0000ff"]\n'
                                '[[profile]]\nname = "SF6"\nsteam_app_id = 1364780\n[profile.led]\ncolors = ["#ff0000"]')
        self.assertEqual(settings.for_profile("SF6").led, led.LedSettings("static", ((255, 0, 0),), 50, 5))
        self.assertEqual(settings.profiles[0].overrides, {"led": {"colors": ["#ff0000"]}})
        self.assertEqual(config.parse(config.render(settings)), settings)
        self.assertEqual(config.overrides_between(settings, settings.for_profile("SF6")),
                         {"led": {"colors": ["#ff0000"]}})


class SaveTests(unittest.TestCase):
    def test_written_file_reads_back_the_same(self):
        settings = config.parse("""
            [gyro]
            button = "fn"
            ratchet = "M2"
            sensitivity = 22.5
            horizontal_scale = 1.25
            invert_y = true
            tightening = 0.5
            [sticks]
            right_deadzone = 0.15
            [remap]
            M1 = "key:ctrl+c"
            C = "mouse:right"
            Z = "NONE"
            RM = "a"
        """)
        self.assertEqual(config.parse(config.render(settings)), settings)
        self.assertEqual(config.parse(config.render(config.Settings())), config.Settings())

    def test_save_keeps_a_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "vader5", "config.toml")
            config.save(config.Settings(), path)
            self.assertFalse(os.path.exists(path + ".bak"))
            changed = config.parse("[gyro]\nsensitivity = 30")
            config.save(changed, path)
            self.assertEqual(config.load(path), changed)
            self.assertEqual(config.load(path + ".bak"), config.Settings())
            self.assertFalse(os.path.exists(path + ".tmp"))


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
