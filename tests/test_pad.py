import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from vader5 import pad, protocol  # noqa: E402


class FakeController:
    info = protocol.ControllerInfo(130, "Vader 5 Pro", "wireless", "7.1.4.0", "40%", 40, False)


class StatusFieldTests(unittest.TestCase):
    def test_connected_controller(self):
        fields = pad.status_fields(FakeController(), active=True, gyro_on=True, profile="Street Fighter 6")
        self.assertEqual(fields, {
            "profile": "Street Fighter 6",
            "connected": True, "active": True, "connection": "wireless", "firmware": "7.1.4.0",
            "battery": "40%", "battery_percent": 40, "charging": False, "gyro": True,
        })

    def test_no_controller(self):
        fields = pad.status_fields(None, active=False, gyro_on=False)
        self.assertFalse(fields["connected"])
        self.assertIsNone(fields["battery_percent"])


if __name__ == "__main__":
    unittest.main()
