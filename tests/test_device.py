import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from vader5 import device  # noqa: E402


class UsbfsTests(unittest.TestCase):
    def test_ioctl_numbers_match_kernel_headers(self):
        self.assertEqual(device.USBDEVFS_DISCONNECT, 0x5516)
        self.assertEqual(device.USBDEVFS_CONNECT, 0x5517)
        if struct.calcsize("P") == 8:
            self.assertEqual(device.USBDEVFS_IOCTL, 0xC0105512)


if __name__ == "__main__":
    unittest.main()
