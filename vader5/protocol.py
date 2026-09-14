"""Flydigi Vader 5 Pro HID protocol.

The controller has a vendor HID interface (usage page 0xFFA0) that accepts
32-byte commands starting with the magic bytes 5A A5. After a short handshake,
"test mode" makes it stream a full input report - every button, both sticks,
triggers, gyro and accelerometer - at about 500 Hz. This works on current
firmware and needs no Flydigi software.

Layouts were checked against a real controller (firmware 7.1.4.0), SDL's
SDL_hidapi_flydigi.c and github.com/BANANASJIM/flydigi-vader5.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

VENDOR_ID = 0x37D7
PRODUCT_ID = 0x2401
CONFIG_DESCRIPTOR_PREFIX = b"\x06\xa0\xff"  # HID item "Usage Page (0xFFA0)"

PACKET_SIZE = 32
MAGIC = b"\x5a\xa5"

CMD_INFO = 0x01
CMD_TEST_MODE = 0x11
CMD_RUMBLE = 0x12
CMD_INPUT_REPORT = 0xEF

INPUT_REPORT_SIZE = 29

GYRO_DPS_PER_UNIT = 2000.0 / 32768.0  # full scale is +/-2000 degrees per second
ACCEL_G_PER_UNIT = 1.0 / 4096.0  # 4096 = 1 g

# (byte offset, bit mask, name) for every known button in the input report.
BUTTON_BITS = (
    (11, 0x01, "UP"), (11, 0x02, "RIGHT"), (11, 0x04, "DOWN"), (11, 0x08, "LEFT"),
    (11, 0x10, "A"), (11, 0x20, "B"), (11, 0x40, "SELECT"), (11, 0x80, "X"),
    (12, 0x01, "Y"), (12, 0x02, "START"), (12, 0x04, "LB"), (12, 0x08, "RB"),
    (12, 0x10, "LT"), (12, 0x20, "RT"),  # digital trigger press, alongside the analog value
    (12, 0x40, "L3"), (12, 0x80, "R3"),
    (13, 0x01, "C"), (13, 0x02, "Z"), (13, 0x04, "M1"), (13, 0x08, "M2"),
    (13, 0x10, "M3"), (13, 0x20, "M4"), (13, 0x40, "LM"), (13, 0x80, "RM"),
    (14, 0x01, "FN"), (14, 0x02, "TURBO"), (14, 0x08, "HOME"),  # TURBO: ~50 ms pulse per press
)
BUTTON_NAMES = tuple(name for _, _, name in BUTTON_BITS)

_KNOWN_MASKS: dict[int, int] = {}
for _offset, _mask, _ in BUTTON_BITS:
    _KNOWN_MASKS[_offset] = _KNOWN_MASKS.get(_offset, 0) | _mask
# Bytes 29-31 aren't buttons: 29 counts packets over the dongle, 31 is a checksum of bytes 2-30.

MODELS = {130: "Vader 5 Pro"}


def command(cmd: int, *args: int) -> bytes:
    """Build a padded command packet. The byte after the arguments is a checksum:
    the sum of every byte after the magic."""
    body = bytes([*MAGIC, cmd, *args])
    body += bytes([sum(body[2:]) & 0xFF])
    return body.ljust(PACKET_SIZE, b"\x00")


CMD_PROFILE_VERSIONS = 0xA1  # which on-board profile is active
CMD_LED_READ = 0xA7
CMD_LED_WRITE_START = 0xA8
CMD_LED_WRITE_PACK = 0xA9
CMD_LED_TEST_COLOR = 0xF5
BLOB_PACKET_SIZE = 20
# There is deliberately no "save" command (0xA6): LED settings are applied live and never written
# to the controller's memory. 0x1F (firmware mode), 0xFD (full reset) and 0xFE must never be sent.


def request(cmd: int, *payload: int) -> bytes:
    """A command with its length byte filled in: 5A A5 cmd length payload checksum."""
    return command(cmd, 2 + len(payload), *payload)


def profile_versions_request() -> bytes:
    return request(CMD_PROFILE_VERSIONS)


def active_profile(reply: bytes) -> int:
    """The active on-board profile (0-3) from a profile-versions reply; 4-7 are the same profiles' Switch mode."""
    raw = reply[5]
    return raw - 4 if 4 <= raw <= 7 else raw if raw <= 3 else 0


def led_read_request(profile: int) -> bytes:
    return request(CMD_LED_READ, profile, BLOB_PACKET_SIZE)


def led_write_start(profile: int, packets: int) -> bytes:
    return request(CMD_LED_WRITE_START, profile, 0, packets, BLOB_PACKET_SIZE)  # start at packet 0


def led_write_pack(index: int, chunk: bytes) -> bytes:
    return request(CMD_LED_WRITE_PACK, index, *chunk)


def led_test_color(red: int, green: int, blue: int) -> bytes:
    """One color on the whole strip right away. Not saved; the next LED upload replaces it."""
    return request(CMD_LED_TEST_COLOR, red & 0xFF, green & 0xFF, blue & 0xFF)


# Handshake sent before enabling test mode; the controller acknowledges each one.
INIT_SEQUENCE = tuple(command(cmd, 0x02) for cmd in (0x01, 0xA1, 0x02, 0x04))


def info_request() -> bytes:
    return command(CMD_INFO, 0x02)


def test_mode(enable: bool) -> bytes:
    return command(CMD_TEST_MODE, 0x07, 0xFF, int(enable), 0xFF, 0xFF, 0xFF)


def rumble(strong: int, weak: int) -> bytes:
    """Strong is the low-frequency (left) motor, weak the high-frequency (right) one; 0-255."""
    return command(CMD_RUMBLE, 0x06, _clamp_byte(strong), _clamp_byte(weak), 0x00, 0x00)


def _clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


@dataclass(frozen=True)
class InputState:
    left_stick: tuple[int, int]  # -32768..32767 per axis, as reported
    right_stick: tuple[int, int]
    left_trigger: int  # 0..255
    right_trigger: int
    buttons: frozenset[str]
    unknown_bits: tuple[tuple[int, int], ...]  # (byte offset, bits) set but not in BUTTON_BITS
    gyro: tuple[int, int, int]  # raw; multiply by GYRO_DPS_PER_UNIT
    accel: tuple[int, int, int]  # raw; multiply by ACCEL_G_PER_UNIT
    raw: bytes

    @property
    def gyro_dps(self) -> tuple[float, float, float]:
        return tuple(v * GYRO_DPS_PER_UNIT for v in self.gyro)

    @property
    def accel_g(self) -> tuple[float, float, float]:
        return tuple(v * ACCEL_G_PER_UNIT for v in self.accel)


@dataclass(frozen=True)
class ControllerInfo:
    device_id: int
    model: str
    connection: str
    firmware: str
    battery: str  # for display: "80%", "60% charging", "full" or "unknown"
    battery_percent: int | None = None  # None when the controller doesn't say
    charging: bool = False


def strip_report_id(data: bytes) -> bytes:
    """Some reads carry a leading report ID byte before the magic; drop it."""
    if len(data) > 1 and data[0] != MAGIC[0] and data[1] == MAGIC[0]:
        return data[1:]
    return data


def parse_input(data: bytes) -> InputState | None:
    if len(data) < INPUT_REPORT_SIZE or data[:2] != MAGIC or data[2] != CMD_INPUT_REPORT:
        return None
    lx, ly, rx, ry = struct.unpack_from("<4h", data, 3)
    gyro = struct.unpack_from("<3h", data, 17)
    accel = struct.unpack_from("<3h", data, 23)
    pressed = frozenset(name for offset, mask, name in BUTTON_BITS if data[offset] & mask)
    unknown = tuple(
        (offset, data[offset] & ~mask & 0xFF)
        for offset, mask in _KNOWN_MASKS.items()
        if data[offset] & ~mask & 0xFF
    )
    return InputState((lx, ly), (rx, ry), data[15], data[16], pressed, unknown, gyro, accel, bytes(data))


def parse_info(data: bytes) -> ControllerInfo | None:
    if len(data) < 17 or data[:2] != MAGIC or data[2] != CMD_INFO:
        return None
    fw = data[15] << 8 | data[16]
    firmware = ".".join(str(fw >> shift & 0xF) for shift in (12, 8, 4, 0))
    connection = {1: "wired", 2: "wireless"}.get(data[6], f"unknown ({data[6]})")
    status, level = data[11] >> 4, data[11] & 0x0F
    percent: int | None
    if status == 2:
        battery, percent = "full", 100
    elif status in (0, 1):
        percent = min(level * 20, 100)
        battery = f"{percent}%" + (" charging" if status == 1 else "")
    else:
        battery, percent = "unknown", None
    model = MODELS.get(data[5], f"unknown model {data[5]}")
    return ControllerInfo(data[5], model, connection, firmware, battery, percent, status == 1)


TESTED_FIRMWARE = "7.1.4.0"
SDL_NATIVE_FIRMWARE = "7.1.4.1"  # SDL's own Vader 5 Pro driver needs at least this (SDL_hidapi_flydigi.c)


def firmware_version(text: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def firmware_note(firmware: str) -> str | None:
    """A warning for firmware this app hasn't been tested with; None for the tested version."""
    if firmware == TESTED_FIRMWARE:
        return None
    version = firmware_version(firmware)
    if version is not None and version >= firmware_version(SDL_NATIVE_FIRMWARE):
        return (f"newer than the tested {TESTED_FIRMWARE}: games that use SDL (and maybe Steam) "
                "may also take over the controller")
    return f"not tested with this app (tested: {TESTED_FIRMWARE})"
