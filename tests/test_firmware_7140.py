"""Real reports recorded from a Vader 5 Pro on firmware 7.1.4.0, over the cable and the dongle (tests/data).

These keep 7.1.4.0 working while support for other firmware is added: if a change breaks how this
firmware's reports are read, these fail. Add tests for new firmware next to these; don't edit these.
"""

import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from evdev import ecodes as e  # noqa: E402

from vader5 import gyro, led, protocol  # noqa: E402
from vader5.virtual_pad import BUTTON_CODES, evdev_values  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.realpath(__file__)), "data")


def load(name: str) -> dict:
    with open(os.path.join(DATA, name)) as file:
        return json.load(file)


def parse(report_hex: str) -> protocol.InputState:
    return protocol.parse_input(bytes.fromhex(report_hex))


class Recording:
    """Tests run on one recording; the subclasses below pick the recording and connection."""

    RECORDING: dict
    CONNECTION: str
    REPORT_S: float  # time between reports

    def all_reports(self) -> list[str]:
        r = self.RECORDING
        return [*r["buttons"].values(), *(item["report"] for item in r["extremes"].values()), *r["resting"], *r["moving"]]

    def feed(self, aim: gyro.GyroAim, reports: list[str], now: float) -> tuple[int, int, float]:
        dx = dy = 0
        states = [parse(report) for report in reports]
        for start in range(0, len(states), 10):
            batch = states[start:start + 10]
            now += len(batch) * self.REPORT_S
            move_x, move_y, _ = aim.process(batch, now)
            dx, dy = dx + move_x, dy + move_y
        return dx, dy, now

    def test_info_reply(self):
        info = protocol.parse_info(bytes.fromhex(self.RECORDING["info_reply"]))
        self.assertEqual((info.model, info.firmware, info.connection), ("Vader 5 Pro", "7.1.4.0", self.CONNECTION))
        self.assertIsNone(protocol.firmware_note(info.firmware))

    def test_every_report_is_understood(self):
        for report in self.all_reports():
            data = bytes.fromhex(report)
            state = protocol.parse_input(data)
            self.assertIsNotNone(state)
            self.assertEqual(state.unknown_bits, ())
            self.check_connection_bytes(data)

    def test_each_button(self):
        self.assertEqual(set(self.RECORDING["buttons"]), set(protocol.BUTTON_NAMES))
        for name, report in self.RECORDING["buttons"].items():
            state = parse(report)
            self.assertEqual(state.buttons, {name})
            keys = {code: value for (kind, code), value in evdev_values(state).items() if kind == e.EV_KEY}
            pressed = {code for code, value in keys.items() if value}
            self.assertEqual(pressed, {BUTTON_CODES[name]} if name in BUTTON_CODES else set(), name)

    def test_sticks_and_triggers_reach_their_ends(self):
        extremes = self.RECORDING["extremes"]
        for stick, index in (("left", 0), ("right", 1)):
            for axis, position in (("x", 0), ("y", 1)):
                for end, expected in (("min", -32768), ("max", 32767)):
                    state = parse(extremes[f"{stick}_{axis}_{end}"]["report"])
                    value = (state.left_stick, state.right_stick)[index][position]
                    self.assertEqual(value, expected, f"{stick} {axis} {end}")
        self.assertEqual(parse(extremes["lt_max"]["report"]).left_trigger, 255)
        self.assertEqual(parse(extremes["rt_max"]["report"]).right_trigger, 255)

    def test_controller_on_the_desk_calibrates_drift(self):
        resting = [parse(report) for report in self.RECORDING["resting"]]
        for state in resting:
            self.assertAlmostEqual(math.sqrt(sum(a * a for a in state.accel_g)), 1.0, delta=0.05)
        aim = gyro.GyroAim()
        now = 0.0
        for _ in range(8):  # the recording is under a second; play it a few times
            _, _, now = self.feed(aim, self.RECORDING["resting"], now)
        drift = [sum(state.gyro_dps[axis] for state in resting) / len(resting) for axis in range(3)]
        for axis in range(3):
            self.assertAlmostEqual(aim.bias[axis], drift[axis], delta=0.05)

    def test_turning_moves_the_mouse(self):
        for space in ("controller", "player"):
            aim = gyro.GyroAim(gyro.GyroSettings(space=space))
            aim.process([parse(self.RECORDING["buttons"]["TURBO"])], 0.0)
            self.assertTrue(aim.enabled)
            dx, dy, _ = self.feed(aim, self.RECORDING["moving"], 0.0)
            self.assertGreater(abs(dx) + abs(dy), 100, space)
            self.assertAlmostEqual(math.sqrt(sum(u * u for u in aim._up)), 1.0, places=6)

    def check_connection_bytes(self, data: bytes) -> None:
        raise NotImplementedError


class Wired7140Tests(Recording, unittest.TestCase):
    RECORDING = load("firmware-7.1.4.0-wired.json")
    CONNECTION = "wired"
    REPORT_S = 1 / 500

    def check_connection_bytes(self, data):
        self.assertEqual(sum(data[2:31]) & 0xFF, data[31])  # checksum, sent over the cable
        self.assertEqual(data[29], 0)

    def test_factory_led_data(self):
        with open(os.path.join(DATA, "firmware-7.1.4.0-led-profile0.bin"), "rb") as file:
            blob = file.read()
        self.assertEqual(led.geometry(blob), (10, 10))
        self.assertEqual((blob[1], blob[8]), (3, 7))  # LED data version 3, effect 7 (factory rainbow)
        built = led.build_blob(led.LedSettings("static", ((255, 0, 0),)), *led.geometry(blob))
        self.assertEqual(len(built), len(blob))
        self.assertEqual(built[:2], blob[:2])


class Wireless7140Tests(Recording, unittest.TestCase):
    RECORDING = load("firmware-7.1.4.0-wireless.json")
    CONNECTION = "wireless"
    REPORT_S = 1 / 420

    def check_connection_bytes(self, data):
        self.assertEqual(data[31], 0)  # no checksum over the dongle

    def test_packet_counter_moves_and_values_repeat(self):
        resting = [bytes.fromhex(report) for report in self.RECORDING["resting"]]
        counters = {data[29] for data in resting}
        self.assertGreater(len(counters), 50)  # byte 29 counts packets over the dongle
        repeats = sum(a[3:29] == b[3:29] for a, b in zip(resting, resting[1:]))
        self.assertGreater(repeats, len(resting) // 4)  # the dongle often repeats the last values


if __name__ == "__main__":
    unittest.main()
