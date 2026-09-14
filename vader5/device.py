"""Finding the controller and talking to its vendor HID interface."""

from __future__ import annotations

import fcntl
import glob
import os
import select
import struct
import time

from . import protocol

READ_SIZE = 64


class DeviceError(RuntimeError):
    """A problem the user can act on: not found, no permission, unplugged."""


def usb_device_dir(hidraw_path: str) -> str:
    """sysfs directory of the USB device (cable or dongle) a hidraw node belongs to."""
    node = os.path.join("/sys/class/hidraw", os.path.basename(hidraw_path), "device")
    return os.path.dirname(os.path.dirname(os.path.realpath(node)))


def find_xpad_event(hidraw_path: str) -> str | None:
    """The basic "Generic X-Box pad" evdev node on the same USB device (interface 0), if any."""
    usb_dir = usb_device_dir(hidraw_path)  # interfaces live inside it, e.g. 5-1.2.3/5-1.2.3:1.0
    interface0 = os.path.join(usb_dir, os.path.basename(usb_dir) + ":1.0") + os.sep
    for event in glob.glob("/sys/class/input/event*"):
        if os.path.realpath(os.path.join(event, "device")).startswith(interface0):
            return "/dev/input/" + os.path.basename(event)
    return None


# usbfs ioctls (linux/usbdevice_fs.h) that detach or reattach the kernel driver of one interface.
_USBDEVFS_IOCTL_STRUCT = "iiP"  # struct usbdevfs_ioctl { int ifno; int ioctl_code; void *data; }
USBDEVFS_IOCTL = 0xC0000000 | (struct.calcsize(_USBDEVFS_IOCTL_STRUCT) << 16) | (ord("U") << 8) | 18
USBDEVFS_DISCONNECT = (ord("U") << 8) | 22
USBDEVFS_CONNECT = (ord("U") << 8) | 23
XPAD_INTERFACE = 0


def usbfs_path(usb_dir: str) -> str:
    """/dev/bus/usb/BBB/DDD node of a USB device."""
    with open(os.path.join(usb_dir, "busnum")) as f:
        bus = int(f.read())
    with open(os.path.join(usb_dir, "devnum")) as f:
        dev = int(f.read())
    return f"/dev/bus/usb/{bus:03d}/{dev:03d}"


def _interface_dir(usb_dir: str, number: int) -> str:
    return os.path.join(usb_dir, f"{os.path.basename(usb_dir)}:1.{number}")


def _interface_driver(usb_dir: str, number: int) -> str | None:
    try:
        return os.path.basename(os.readlink(os.path.join(_interface_dir(usb_dir, number), "driver")))
    except OSError:
        return None


def _usbfs_interface_ioctl(usb_dir: str, code: int) -> None:
    fd = os.open(usbfs_path(usb_dir), os.O_RDWR)
    try:
        request = bytearray(struct.pack(_USBDEVFS_IOCTL_STRUCT, XPAD_INTERFACE, code, 0))
        fcntl.ioctl(fd, USBDEVFS_IOCTL, request)
    finally:
        os.close(fd)


def detach_xpad(hidraw_path: str) -> str | None:
    """Take the kernel's xpad driver off interface 0, so the basic Xbox pad disappears everywhere.

    Returns the USB device directory to pass to reattach_xpad() - also when xpad was already
    detached (e.g. after a crash) - or None if interface 0 isn't an xpad interface.
    Raises PermissionError without access to the USB device (see vader5/data/70-vader5-pro.rules).
    """
    usb_dir = usb_device_dir(hidraw_path)
    driver = _interface_driver(usb_dir, XPAD_INTERFACE)
    if driver == "xpad":
        _usbfs_interface_ioctl(usb_dir, USBDEVFS_DISCONNECT)
        return usb_dir
    if driver is None and os.path.isdir(_interface_dir(usb_dir, XPAD_INTERFACE)):
        return usb_dir
    return None


def reattach_xpad(usb_dir: str) -> None:
    """Give interface 0 back to the kernel, restoring the basic Xbox pad. Does nothing if it has a driver."""
    if _interface_driver(usb_dir, XPAD_INTERFACE) is None:
        _usbfs_interface_ioctl(usb_dir, USBDEVFS_CONNECT)


def find_config_hidraws() -> list[tuple[str, str]]:
    """Every connected Flydigi config interface (cable or dongle) as (/dev/hidrawN, description)."""
    found = []
    for node in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            with open(os.path.join(node, "device", "uevent")) as f:
                uevent = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
            with open(os.path.join(node, "device", "report_descriptor"), "rb") as f:
                descriptor = f.read()
        except OSError:
            continue
        parts = uevent.get("HID_ID", "").split(":")  # bus:vendor:product
        if len(parts) != 3 or int(parts[1], 16) != protocol.VENDOR_ID:
            continue
        if not descriptor.startswith(protocol.CONFIG_DESCRIPTOR_PREFIX):
            continue
        usb_device = usb_device_dir(node)
        description = (
            f"{uevent.get('HID_NAME', 'Flydigi controller')}"
            f"  (USB {int(parts[1], 16):04x}:{int(parts[2], 16):04x}, port {os.path.basename(usb_device)})"
        )
        found.append(("/dev/" + os.path.basename(node), description))
    return sorted(found, key=lambda item: int(item[0].removeprefix("/dev/hidraw")))


def find_config_hidraw() -> str:
    """Return the /dev/hidrawN node of the controller's vendor (config) interface."""
    found = find_config_hidraws()
    if not found:
        raise DeviceError(
            "Flydigi Vader 5 Pro not found. Plug in the cable or the 2.4G dongle and turn the controller on."
        )
    if len(found) > 1:
        choices = "\n".join(f"  {path}  {description}" for path, description in found)
        raise DeviceError(f"More than one Flydigi connection found; choose one with --device:\n{choices}")
    return found[0][0]


def switch_off(path: str | None = None) -> None:
    """Turn test mode and rumble off without a handshake, e.g. after a crash left them on."""
    path = path or find_config_hidraw()
    try:
        fd = os.open(path, os.O_WRONLY)
    except PermissionError:
        raise DeviceError(f"No permission to open {path}. See 'Permissions' in README.md.") from None
    try:
        os.write(fd, protocol.rumble(0, 0))
        os.write(fd, protocol.test_mode(False))
    except OSError as e:
        raise DeviceError("Lost connection to the controller (unplugged or powered off).") from e
    finally:
        os.close(fd)
    try:
        reattach_xpad(usb_device_dir(path))  # also bring back a basic Xbox pad a crash left removed
    except OSError:
        pass


class Controller:
    """The controller in test mode.

    Use it as a context manager so test mode and rumble are always switched off again.
    """

    REPLY_TIMEOUT_S = 0.1
    WATCHDOG_S = 0.5  # re-send test mode if reports stop arriving

    def __init__(self, path: str | None = None):
        self.path = path or find_config_hidraw()
        self.info: protocol.ControllerInfo | None = None
        self._fd: int | None = None
        self._last_report = 0.0

    def __enter__(self) -> Controller:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise DeviceError(
                f"No permission to open {self.path}. See 'Permissions' in README.md."
            ) from None
        except FileNotFoundError:
            raise DeviceError(f"{self.path} does not exist. Is the controller still connected?") from None

        self._read_available()  # discard anything stale
        for packet in protocol.INIT_SEQUENCE:
            self._write(packet)
            if not self._wait_for_reply():
                self.close()
                raise DeviceError(
                    "The controller isn't answering. If you're using the dongle, is the controller switched on?"
                )
        self._write(protocol.test_mode(True))
        self._last_report = time.monotonic()

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            self._write(protocol.rumble(0, 0))
            self._write(protocol.test_mode(False))
        except DeviceError:
            pass  # already unplugged; nothing to switch off
        finally:
            os.close(self._fd)
            self._fd = None

    def fileno(self) -> int:
        """Lets callers select() on the controller together with other devices."""
        return self._fd

    def poll(self, timeout: float) -> list[protocol.InputState]:
        """Wait up to `timeout` seconds and return every input report that arrived."""
        select.select([self._fd], [], [], max(0.0, timeout))
        states = [s for s in map(protocol.parse_input, self._read_available()) if s]
        now = time.monotonic()
        if states:
            self._last_report = now
        elif now - self._last_report > self.WATCHDOG_S:
            self._write(protocol.test_mode(True))
            self._last_report = now
        return states

    BLOB_TIMEOUT_S = 3.0

    def _replies(self, cmd: int, timeout: float):
        """Replies to `cmd` arriving within `timeout`. Input reports read meanwhile are dropped."""
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            select.select([self._fd], [], [], min(remaining, 0.05))
            for packet in self._read_available():
                if len(packet) >= 5 and packet[:2] == protocol.MAGIC and packet[2] == cmd:
                    yield packet

    def _exchange(self, packet: bytes, cmd: int, timeout: float = 1.0, attempts: int = 3) -> bytes | None:
        for _ in range(attempts):
            self._write(packet)
            for reply in self._replies(cmd, timeout):
                return reply
        return None

    def read_active_profile(self) -> int | None:
        """Which on-board profile (0-3) the controller is using; None if it doesn't answer."""
        reply = self._exchange(protocol.profile_versions_request(), protocol.CMD_PROFILE_VERSIONS)
        return protocol.active_profile(reply) if reply else None

    def read_led(self, profile: int) -> bytes | None:
        """An on-board profile's LED settings. Only read the active profile: reading one selects it."""
        size = protocol.BLOB_PACKET_SIZE
        for _ in range(3):
            self._write(protocol.led_read_request(profile))
            packets: dict[int, bytes] = {}
            for reply in self._replies(protocol.CMD_LED_READ, self.BLOB_TIMEOUT_S):
                total = reply[3]
                packets[reply[4]] = bytes(reply[6:6 + size])
                if total and all(index in packets for index in range(total)):
                    return b"".join(packets[index] for index in range(total))
        return None

    def write_led(self, profile: int, blob: bytes) -> bool:
        """Send LED settings. They show at once and are NOT saved: a power cycle brings back the stored lights."""
        size = protocol.BLOB_PACKET_SIZE
        chunks = [blob[i:i + size] for i in range(0, len(blob), size)]
        if self._exchange(protocol.led_write_start(profile, len(chunks)), protocol.CMD_LED_WRITE_START) is None:
            return False
        return all(
            self._exchange(protocol.led_write_pack(index, chunk), protocol.CMD_LED_WRITE_PACK) is not None
            for index, chunk in enumerate(chunks)
        )

    def set_led_color(self, red: int, green: int, blue: int) -> None:
        """Show one color on the whole LED strip right away, without waiting for the reply."""
        self._write(protocol.led_test_color(red, green, blue))

    def request_info(self) -> None:
        """Ask for firmware/connection/battery; the answer shows up in `self.info` after a poll."""
        self._write(protocol.info_request())

    def rumble(self, strong: int, weak: int) -> None:
        self._write(protocol.rumble(strong, weak))

    def _wait_for_reply(self) -> bool:
        deadline = time.monotonic() + self.REPLY_TIMEOUT_S
        while (remaining := deadline - time.monotonic()) > 0:
            select.select([self._fd], [], [], remaining)
            if any(packet[:2] == protocol.MAGIC for packet in self._read_available()):
                return True
        return False

    def _read_available(self) -> list[bytes]:
        packets = []
        for _ in range(512):  # bounded so a fast stream can't keep us here forever
            try:
                data = os.read(self._fd, READ_SIZE)
            except BlockingIOError:
                break
            except OSError as e:
                raise DeviceError("Lost connection to the controller (unplugged or powered off).") from e
            if not data:
                break
            data = protocol.strip_report_id(data)
            info = protocol.parse_info(data)
            if info:
                self.info = info
            packets.append(data)
        return packets

    def _write(self, packet: bytes) -> None:
        try:
            os.write(self._fd, packet)
        except OSError as e:
            raise DeviceError("Lost connection to the controller (unplugged or powered off).") from e
