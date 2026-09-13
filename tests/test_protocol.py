import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from vader5 import protocol  # noqa: E402

# Captured from the real controller (firmware 7.1.4.0, wired) on 2026-09-12.
REAL_INFO_REPLY = bytes.fromhex(
    "5aa5010100820100000000254501007140000035150000000000001026 1f0040".replace(" ", "")
)


REAL_DONGLE_INFO_REPLY = bytes.fromhex("5aa501010082020000000005450100714004673515000000000000102" "61f0021")


def input_report(**fields) -> bytes:
    data = bytearray(32)
    data[0:3] = b"\x5a\xa5\xef"
    struct.pack_into("<4h", data, 3, *fields.get("sticks", (0, 0, 0, 0)))
    for offset, value in fields.get("bytes", {}).items():
        data[offset] = value
    struct.pack_into("<3h", data, 17, *fields.get("gyro", (0, 0, 0)))
    struct.pack_into("<3h", data, 23, *fields.get("accel", (0, 0, 4096)))
    return bytes(data)


class CommandTests(unittest.TestCase):
    def test_handshake_matches_known_packets(self):
        expected = ["5aa5010203", "5aa5a102a3", "5aa5020204", "5aa5040206"]
        self.assertEqual([p[:5].hex() for p in protocol.INIT_SEQUENCE], expected)
        self.assertTrue(all(len(p) == 32 for p in protocol.INIT_SEQUENCE))

    def test_test_mode_checksums(self):
        self.assertEqual(protocol.test_mode(True)[:10].hex(), "5aa51107ff01ffffff15")
        self.assertEqual(protocol.test_mode(False)[:10].hex(), "5aa51107ff00ffffff14")

    def test_rumble_packet(self):
        packet = protocol.rumble(0x40, 0x30)
        self.assertEqual(packet[:9].hex(), "5aa512064030000088")
        self.assertEqual(protocol.rumble(999, -5)[4:6], b"\xff\x00")


class ParseTests(unittest.TestCase):
    def test_real_info_reply(self):
        info = protocol.parse_info(REAL_INFO_REPLY)
        self.assertEqual(info.model, "Vader 5 Pro")
        self.assertEqual(info.connection, "wired")
        self.assertEqual(info.firmware, "7.1.4.0")
        self.assertEqual(info.battery, "full")

    def test_battery_fields(self):
        cable = protocol.parse_info(REAL_INFO_REPLY)
        self.assertEqual((cable.battery, cable.battery_percent, cable.charging), ("full", 100, False))
        dongle = protocol.parse_info(REAL_DONGLE_INFO_REPLY)
        self.assertEqual((dongle.connection, dongle.battery, dongle.battery_percent, dongle.charging),
                         ("wireless", "100%", 100, False))
        charging = bytearray(REAL_DONGLE_INFO_REPLY)
        charging[11] = 0x13  # charging, level 3
        info = protocol.parse_info(bytes(charging))
        self.assertEqual((info.battery, info.battery_percent, info.charging), ("60% charging", 60, True))

    def test_input_report_values(self):
        state = protocol.parse_input(input_report(
            sticks=(100, -200, 32767, -32768),
            bytes={11: 0x10 | 0x01, 12: 0x04, 13: 0x04 | 0x20 | 0x01, 14: 0x08, 15: 255, 16: 7},
            gyro=(16384, 0, -16384),
            accel=(0, 0, 4096),
        ))
        self.assertEqual(state.left_stick, (100, -200))
        self.assertEqual(state.right_stick, (32767, -32768))
        self.assertEqual((state.left_trigger, state.right_trigger), (255, 7))
        self.assertEqual(state.buttons, {"A", "UP", "LB", "M1", "M4", "C", "HOME"})
        self.assertEqual(state.unknown_bits, ())
        self.assertAlmostEqual(state.gyro_dps[0], 1000.0)
        self.assertAlmostEqual(state.gyro_dps[2], -1000.0)
        self.assertAlmostEqual(state.accel_g[2], 1.0)

    def test_digital_trigger_bits(self):
        state = protocol.parse_input(input_report(bytes={12: 0x10 | 0x20, 15: 255, 16: 255}))
        self.assertEqual(state.buttons, {"LT", "RT"})
        self.assertEqual(state.unknown_bits, ())

    def test_fn_and_turbo(self):
        state = protocol.parse_input(input_report(bytes={14: 0x01 | 0x02}))
        self.assertEqual(state.buttons, {"FN", "TURBO"})

    def test_unknown_bits_are_reported(self):
        state = protocol.parse_input(input_report(bytes={14: 0x10 | 0x20}))
        self.assertEqual(state.buttons, frozenset())
        self.assertEqual(state.unknown_bits, ((14, 0x30),))

    def test_packet_counter_and_checksum_are_not_buttons(self):
        state = protocol.parse_input(input_report(bytes={29: 0x65, 31: 0x2B}))
        self.assertEqual(state.unknown_bits, ())

    def test_rejects_other_packets(self):
        self.assertIsNone(protocol.parse_input(REAL_INFO_REPLY))
        self.assertIsNone(protocol.parse_input(b"\x5a\xa5\xef"))
        self.assertIsNone(protocol.parse_info(input_report()))

    def test_strip_report_id(self):
        self.assertEqual(protocol.strip_report_id(b"\x03\x5a\xa5\xef"), b"\x5a\xa5\xef")
        self.assertEqual(protocol.strip_report_id(b"\x5a\xa5\xef"), b"\x5a\xa5\xef")


if __name__ == "__main__":
    unittest.main()
