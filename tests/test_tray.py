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


if __name__ == "__main__":
    unittest.main()
