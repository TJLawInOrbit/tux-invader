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



class RumbleSenderTests(unittest.TestCase):
    def test_a_flood_of_changes_is_limited_and_the_latest_level_wins(self):
        sender = pad.RumbleSender()
        sent = []
        for frame in range(300):  # an emulator changing rumble 300 times in one second
            now = frame / 300
            level = (100 + frame % 50, 0)
            sender.note_change()
            result = sender.update(level, now)
            if result is not None:
                sent.append((now, result))
        self.assertLessEqual(len(sent), 21)  # about 20 a second, not 300
        gaps = [b[0] - a[0] for a, b in zip(sent, sent[1:])]
        self.assertGreaterEqual(min(gaps), pad.RUMBLE_MIN_GAP_S - 1e-9)

    def test_active_rumble_is_refreshed(self):
        sender = pad.RumbleSender()
        self.assertEqual(sender.update((80, 40), 0.0), (80, 40))
        self.assertIsNone(sender.update((80, 40), 0.3))
        self.assertEqual(sender.update((80, 40), 0.6), (80, 40))  # re-sent so it never lapses

    def test_a_stop_is_repeated_for_a_while_then_it_goes_quiet(self):
        sender = pad.RumbleSender()
        sender.update((200, 200), 0.0)
        sends = [now for now in (x / 100 for x in range(10, 400)) if sender.update((0, 0), now) is not None]
        self.assertEqual(sends[0], 0.1)  # the stop goes out at once...
        self.assertGreaterEqual(len(sends), 5)  # ...and again several times...
        self.assertLess(sends[-1], 0.1 + pad.RUMBLE_STOP_REPEAT_FOR_S + 0.01)  # ...then nothing more is sent

    def test_busy_rumble_is_reported_once_a_minute(self):
        sender = pad.RumbleSender(started=0.0)
        for _ in range(pad.RUMBLE_BUSY + 1):
            sender.note_change()
        self.assertIsNone(sender.report(30.0))  # not a minute yet
        self.assertIn(f"changed it {pad.RUMBLE_BUSY + 1} times", sender.report(60.0))
        self.assertIsNone(sender.report(121.0))  # a quiet minute: nothing to say


if __name__ == "__main__":
    unittest.main()
