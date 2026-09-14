import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from vader5 import status  # noqa: E402


class StatusFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.folder.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_write_read_remove(self):
        self.assertIsNone(status.read())
        status.write({"connected": True, "battery_percent": 80})
        current = status.read()
        self.assertEqual((current["connected"], current["battery_percent"]), (True, 80))
        self.assertTrue(status.status_path().startswith(self.folder.name))
        status.remove()
        status.remove()  # removing twice is fine
        self.assertIsNone(status.read())

    def test_old_status_counts_as_not_running(self):
        status.write({"connected": True})
        self.assertIsNotNone(status.read(now=time.time() + status.STALE_S - 1))
        self.assertIsNone(status.read(now=time.time() + status.STALE_S + 1))

    def test_broken_file_counts_as_not_running(self):
        os.makedirs(os.path.dirname(status.status_path()), exist_ok=True)
        with open(status.status_path(), "w") as f:
            f.write("{not json")
        self.assertIsNone(status.read())

    def test_second_instance_is_refused_until_first_exits(self):
        first = status.single_instance_lock("test")
        self.assertIsNotNone(first)
        self.assertIsNone(status.single_instance_lock("test"))
        self.assertIsNotNone(other := status.single_instance_lock("other"))  # different names don't clash
        other.close()
        first.close()
        again = status.single_instance_lock("test")
        self.assertIsNotNone(again)
        again.close()


class BatteryTextTests(unittest.TestCase):
    def test_battery_text(self):
        self.assertEqual(status.battery_text({"battery": "full", "battery_percent": 100}), "full")
        self.assertEqual(status.battery_text({"battery": "80%", "battery_percent": 80}), "80%")
        self.assertEqual(status.battery_text({"battery_percent": 60, "charging": True}), "60%, charging")
        self.assertIsNone(status.battery_text({"battery": None, "battery_percent": None}))



class LockHolderTests(unittest.TestCase):
    def test_the_lock_holder_is_found_by_its_lock(self):
        import tempfile
        from unittest import mock
        from vader5 import status
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(status, "runtime_dir", lambda: folder):
            self.assertIsNone(status.lock_holder("vader5-tray"))  # no lock file yet
            lock = status.single_instance_lock("vader5-tray")
            self.assertEqual(status.lock_holder("vader5-tray"), os.getpid())
            self.assertIsNone(status.single_instance_lock("vader5-tray"))  # a second one isn't allowed...
            self.assertEqual(status.lock_holder("vader5-tray"), os.getpid())  # ...and doesn't wipe the ID
            lock.close()
            self.assertIsNone(status.lock_holder("vader5-tray"))


if __name__ == "__main__":
    unittest.main()
