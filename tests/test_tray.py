import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PyQt6  # noqa: F401
except ImportError:
    PyQt6 = None


def connected(**fields):
    base = {"connected": True, "active": True, "connection": "wireless", "battery": "80%",
            "battery_percent": 80, "charging": False, "gyro": False}
    return {**base, **fields}


@unittest.skipIf(PyQt6 is None, "PyQt6 isn't installed")
class DescribeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from vader5 import tray
        cls.tray = tray

    def test_service_not_running(self):
        view = self.tray.describe(None)
        self.assertFalse(view.service_running)
        self.assertFalse(view.active)
        self.assertIn("isn't running", view.tooltip)

    def test_controller_not_connected(self):
        view = self.tray.describe({"connected": False})
        self.assertTrue(view.service_running)
        self.assertEqual(view.lines[0], "Controller: not connected")

    def test_connected(self):
        view = self.tray.describe(connected(gyro=True))
        self.assertTrue(view.active)
        self.assertEqual(view.lines, ["Controller: connected (wireless)", "Battery: 80%", "Gyro aiming: on"])
        self.assertEqual(view.tooltip, "The Tux InVader: wireless · battery 80% · gyro on")

    def test_idle_and_charging(self):
        view = self.tray.describe(connected(active=False, connection="wired", charging=True, battery_percent=60))
        self.assertFalse(view.active)
        self.assertEqual(view.lines[:2], ["Controller: connected (wired), idle", "Battery: 60%, charging"])

    def test_low_battery_warns_once_until_charged(self):
        warner = self.tray.LowBatteryWarner(threshold=20)
        self.assertFalse(warner.check(connected(battery_percent=40)))
        self.assertTrue(warner.check(connected(battery_percent=20)))
        self.assertFalse(warner.check(connected(battery_percent=20)))  # already warned
        self.assertFalse(warner.check(None))  # service briefly gone: still remembers
        self.assertFalse(warner.check(connected(battery_percent=0)))
        self.assertFalse(warner.check(connected(battery_percent=20, charging=True)))  # charging resets it
        self.assertTrue(warner.check(connected(battery_percent=20)))



@unittest.skipIf(PyQt6 is None, "PyQt6 isn't installed")
class WarningSoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from vader5 import tray
        cls.tray = tray

    def test_the_best_battery_sound_is_found_in_the_sound_themes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as data:
            for theme, name in (("freedesktop", "dialog-warning.oga"), ("ocean", "battery-caution.oga")):
                os.makedirs(os.path.join(data, "sounds", theme, "stereo"), exist_ok=True)
                open(os.path.join(data, "sounds", theme, "stereo", name), "w").close()
            self.assertTrue(self.tray.warning_sound_file([data]).endswith("ocean/stereo/battery-caution.oga"))
            self.assertIsNone(self.tray.warning_sound_file([os.path.join(data, "nothing here")]))

    def test_it_plays_with_a_player_the_system_has(self):
        have = {"paplay"}
        which = lambda name: f"/usr/bin/{name}" if name in have else None  # noqa: E731
        self.assertEqual(self.tray.warning_sound_command("/s/b.oga", which), ["paplay", "/s/b.oga"])
        have.add("pw-play")
        self.assertEqual(self.tray.warning_sound_command("/s/b.oga", which), ["pw-play", "/s/b.oga"])
        self.assertIsNone(self.tray.warning_sound_command(None, which))
        self.assertIsNone(self.tray.warning_sound_command("/s/b.oga", lambda name: None))

    def test_the_sound_can_be_turned_off(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.toml")
            self.assertTrue(self.tray.low_battery_sound_wanted(path))  # no settings file: on
            with open(path, "w") as file:
                file.write("[notifications]\nlow_battery_sound = false\n")
            self.assertFalse(self.tray.low_battery_sound_wanted(path))
            with open(path, "w") as file:
                file.write("[notifications]\nlow_battery_sound = maybe\n")
            self.assertTrue(self.tray.low_battery_sound_wanted(path))  # a mistake in the file: the default


if __name__ == "__main__":
    unittest.main()
