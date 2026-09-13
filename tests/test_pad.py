import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from vader5 import pad  # noqa: E402


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_refused_until_first_exits(self):
        with tempfile.TemporaryDirectory() as runtime, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
            first = pad.single_instance_lock()
            self.assertIsNotNone(first)
            self.assertIsNone(pad.single_instance_lock())
            first.close()
            again = pad.single_instance_lock()
            self.assertIsNotNone(again)
            again.close()


if __name__ == "__main__":
    unittest.main()
