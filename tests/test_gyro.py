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

    def test_hold_mode_aims_only_while_the_button_is_held(self):
        aim = gyro.GyroAim(gyro.GyroSettings(button="Z", mode="hold", sensitivity=10.0))
        turning = report(gyro_dps=(0, 0, 90))
        held = report(gyro_dps=(0, 0, 90), buttons={"Z"})
        before, _, now = self.feed(aim, turning, 0.5)
        self.assertEqual(before, 0)  # not held: no aiming
        self.assertTrue(aim.process([held], now + 0.002)[2])  # pressing it reports a change (for the buzz)
        moved, _, now = self.feed(aim, held, 0.5, start=now + 0.002)
        self.assertAlmostEqual(abs(moved), 440, delta=20)
        self.assertTrue(aim.enabled)
        self.assertTrue(aim.process([turning], now + 0.002)[2])  # letting go reports a change too
        after, _, _ = self.feed(aim, turning, 0.5, start=now + 0.002)
        self.assertEqual(after, 0)
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

    def test_direction_scales(self):
        turn_and_tilt = report(gyro_dps=(40, 0, 40))
        base_x, base_y, _ = self.feed(self.enabled_aim(sensitivity=10.0), turn_and_tilt, 0.5, start=0.002)
        x, y, _ = self.feed(self.enabled_aim(sensitivity=10.0, horizontal_scale=1.5), turn_and_tilt, 0.5, start=0.002)
        self.assertAlmostEqual(x, base_x * 1.5, delta=3)
        self.assertAlmostEqual(y, base_y, delta=1)  # vertical unchanged

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

    def test_drift_is_calibrated_out_while_resting(self):
        aim = gyro.GyroAim()
        drift = (0.3, -0.2, 0.3)
        _, _, now = self.feed(aim, report(gyro_dps=drift), 10.0)
        aim.process([report(gyro_dps=drift, buttons={BUTTON})], now + 0.002)
        dx, dy, _ = self.feed(aim, report(gyro_dps=drift), 2.0, start=now + 0.002)
        self.assertLessEqual(abs(dx) + abs(dy), 1)  # uncalibrated this would be about 9 counts

    def feed_alternating(self, aim, states, seconds, start=0.0, rate=490.0):
        """Feed reports that alternate between `states`, like shaky hands, in batches of 10."""
        now, dx = start, 0
        for index in range(int(seconds * rate / 10)):
            now += 10 / rate
            batch = [states[(index * 10 + i) % len(states)] for i in range(10)]
            dx += aim.process(batch, now)[0]
        return dx, now

    def test_resting_noise_still_calibrates(self):
        aim = gyro.GyroAim()
        desk = [report(gyro_dps=(0.2, 0, 0.4)), report(gyro_dps=(0.0, 0, 0.2))]  # 0.1 deg/s wobble
        self.feed_alternating(aim, desk, 10.0)
        self.assertAlmostEqual(aim.bias[2], 0.3, delta=0.05)

    def test_slow_aiming_in_hands_is_not_taken_for_drift(self):
        aim = self.enabled_aim(sensitivity=100.0, tightening_dps=0.0)
        hands = [report(gyro_dps=(0, 0, 1.3)), report(gyro_dps=(0, 0, 0.1))]  # 0.7 deg/s aim, shaking 0.6
        dx, _ = self.feed_alternating(aim, hands, 6.0, start=0.002)
        self.assertAlmostEqual(abs(dx), 0.7 * 6 * 100, delta=20)  # every bit of it reaches the mouse
        self.assertEqual(aim.bias, [0.0, 0.0, 0.0])

    def test_player_space_turns_at_full_speed_when_tilted(self):
        tilted_up = (0.0, 0.6, 0.8)  # the controller's face tipped toward you
        turning = report(gyro_dps=tuple(90 * u for u in (0.0, 0.6, 0.8)), accel_g=tilted_up)  # turning in place
        own_axis, dy, _ = self.feed(self.enabled_aim(sensitivity=10.0), turning, 1.0, start=0.002)
        player, player_dy, _ = self.feed(self.enabled_aim(sensitivity=10.0, space="player"), turning, 1.0, start=0.002)
        self.assertAlmostEqual(abs(own_axis), 900 * 0.8, delta=15)
        self.assertAlmostEqual(abs(player), 900, delta=15)
        self.assertEqual((dy, player_dy), (0, 0))
        self.assertEqual(own_axis > 0, player > 0)  # same direction

    def test_player_space_matches_controller_space_when_flat(self):
        turning = report(gyro_dps=(0, 0, 90))
        own_axis, _, _ = self.feed(self.enabled_aim(sensitivity=10.0), turning, 1.0, start=0.002)
        player, _, _ = self.feed(self.enabled_aim(sensitivity=10.0, space="player"), turning, 1.0, start=0.002)
        self.assertAlmostEqual(player, own_axis, delta=2)

    def test_up_direction_follows_rotation(self):
        aim = gyro.GyroAim()
        aim.process([report()], 0.0)  # flat: up is the controller's z axis
        # tip it 90 degrees around the pitch axis while shaking (1.5 g), so only the gyro can tell
        states = [report(gyro_dps=(90, 0, 0), accel_g=(0.0, 0.0, 1.5))] * 10
        now = 0.0
        for _ in range(50):  # 1 second
            now += 0.02
            aim.process(states, now)
        self.assertAlmostEqual(abs(aim._up[1]), 1.0, delta=0.05)  # up now lies along the roll axis

    def test_ratchet_pauses_movement_while_held(self):
        aim = self.enabled_aim(sensitivity=10.0, ratchet="M2")
        turning = report(gyro_dps=(0, 0, 90))
        held = report(gyro_dps=(0, 0, 90), buttons={"M2"})
        moved, _, now = self.feed(aim, turning, 0.5, start=0.002)
        paused, _, now = self.feed(aim, held, 0.5, start=now)
        resumed, _, _ = self.feed(aim, turning, 0.5, start=now)
        self.assertAlmostEqual(abs(moved), 440, delta=15)  # 90 deg/s for ~0.49 s at 10 counts per degree
        self.assertEqual(paused, 0)
        self.assertAlmostEqual(abs(resumed), 440, delta=15)
        self.assertTrue(aim.enabled)  # pausing doesn't switch gyro aiming off

    def test_long_gap_cannot_cause_a_jump(self):
        aim = self.enabled_aim(sensitivity=10.0)
        dx, _, _ = aim.process([report(gyro_dps=(0, 0, 100))], 10.0)
        self.assertLessEqual(abs(dx), 100 * gyro.MAX_BATCH_S * 10 + 1)


if __name__ == "__main__":
    unittest.main()
