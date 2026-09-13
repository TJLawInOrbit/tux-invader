import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from test_protocol import input_report  # noqa: E402
from vader5 import gyro, protocol  # noqa: E402

BUTTON = gyro.GyroSettings().button  # the default gyro toggle


def report(gyro_dps=(0.0, 0.0, 0.0), buttons=(), accel_g=(0.0, 0.0, 1.0)):
    """An input state with the given rotation (pitch, roll, yaw in degrees/s), buttons and gravity."""
    button_bytes = {}
    for offset, mask, name in protocol.BUTTON_BITS:
        if name in buttons:
            button_bytes[offset] = button_bytes.get(offset, 0) | mask
    return protocol.parse_input(input_report(
        gyro=tuple(round(v / protocol.GYRO_DPS_PER_UNIT) for v in gyro_dps),
        accel=tuple(round(v / protocol.ACCEL_G_PER_UNIT) for v in accel_g),
        bytes=button_bytes,
    ))


class GyroAimTests(unittest.TestCase):
    def feed(self, aim, state, seconds, start=0.0, batch=10, rate=490.0):
        """Feed `seconds` of identical reports in batches, like the controller does. Returns (dx, dy, now)."""
        now, dx, dy = start, 0, 0
        for _ in range(int(seconds * rate / batch)):
            now += batch / rate
            move_x, move_y, _ = aim.process([state] * batch, now)
            dx, dy = dx + move_x, dy + move_y
        return dx, dy, now

    def enabled_aim(self, **settings):
        aim = gyro.GyroAim(gyro.GyroSettings(**settings))
        aim.process([report(buttons={BUTTON})], 0.0)
        aim.process([report()], 0.002)
        self.assertTrue(aim.enabled)
        return aim

    def test_button_toggles_on_each_press(self):
        aim = gyro.GyroAim()
        self.assertTrue(aim.process([report(buttons={BUTTON})], 0.01)[2])
        self.assertTrue(aim.enabled)
        self.assertFalse(aim.process([report(buttons={BUTTON})], 0.02)[2])  # still held: no second toggle
        aim.process([report()], 0.03)
        self.assertTrue(aim.process([report(buttons={BUTTON})], 0.04)[2])
        self.assertFalse(aim.enabled)

    def test_no_movement_while_off(self):
        dx, dy, _ = self.feed(gyro.GyroAim(), report(gyro_dps=(0, 0, 90)), 1.0)
        self.assertEqual((dx, dy), (0, 0))

    def test_turning_moves_sideways_only(self):
        aim = self.enabled_aim(sensitivity=10.0)
        dx, dy, _ = self.feed(aim, report(gyro_dps=(0, 0, 90)), 1.0, start=0.002)
        self.assertAlmostEqual(abs(dx), 900, delta=15)  # 90 degrees x 10 counts per degree
        self.assertEqual(dy, 0)

    def test_tilting_moves_vertically_only(self):
        aim = self.enabled_aim(sensitivity=10.0)
        dx, dy, _ = self.feed(aim, report(gyro_dps=(60, 0, 0)), 1.0, start=0.002)
        self.assertEqual(dx, 0)
        self.assertAlmostEqual(abs(dy), 600, delta=15)

    def test_invert_flips_direction(self):
        normal, _, _ = self.feed(self.enabled_aim(), report(gyro_dps=(0, 0, 45)), 0.5, start=0.002)
        inverted, _, _ = self.feed(self.enabled_aim(invert_x=True), report(gyro_dps=(0, 0, 45)), 0.5, start=0.002)
        self.assertNotEqual(normal, 0)
        self.assertAlmostEqual(inverted, -normal, delta=2)

    def test_slow_rotation_is_tightened(self):
        shaking = (0.0, 0.0, 1.5)  # gravity off 1 g, so drift calibration stays out of this test
        slow = report(gyro_dps=(0, 0, 0.5), accel_g=shaking)
        loose, _, _ = self.feed(self.enabled_aim(sensitivity=100.0, tightening_dps=0.0), slow, 1.0, start=0.002)
        tight, _, _ = self.feed(self.enabled_aim(sensitivity=100.0, tightening_dps=1.0), slow, 1.0, start=0.002)
        self.assertAlmostEqual(abs(loose), 50, delta=3)
        self.assertAlmostEqual(abs(tight), 25, delta=3)

    def test_drift_is_calibrated_out_while_still(self):
        aim = gyro.GyroAim()
        drift = (0.3, -0.2, 0.3)
        _, _, now = self.feed(aim, report(gyro_dps=drift), 4.0)
        aim.process([report(gyro_dps=drift, buttons={BUTTON})], now + 0.002)
        dx, dy, _ = self.feed(aim, report(gyro_dps=drift), 2.0, start=now + 0.002)
        self.assertLessEqual(abs(dx) + abs(dy), 1)  # uncalibrated this would be about 9 counts

    def test_long_gap_cannot_cause_a_jump(self):
        aim = self.enabled_aim(sensitivity=10.0)
        dx, _, _ = aim.process([report(gyro_dps=(0, 0, 100))], 10.0)
        self.assertLessEqual(abs(dx), 100 * gyro.MAX_BATCH_S * 10 + 1)


if __name__ == "__main__":
    unittest.main()
