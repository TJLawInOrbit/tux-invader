import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no screen needed

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # the settings window is optional
    QApplication = None

from vader5 import config, protocol  # noqa: E402


@unittest.skipIf(QApplication is None, "PyQt6 isn't installed")
class SettingsWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from vader5 import settings_window
        cls.module = settings_window
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "config.toml")
        config.save(config.parse('[gyro]\nratchet = "M2"\nhorizontal_scale = 1.25\n[remap]\nM1 = "key:space"\nC = "mouse:right"'), self.path)
        self.window = self.module.SettingsWindow(self.path, manage_service=False)

    def tearDown(self):
        self.window.save_button.setEnabled(False)  # don't ask about unsaved changes
        self.window.close()
        self.folder.cleanup()

    def test_every_button_is_listed(self):
        self.assertEqual(set(self.module.UI_ORDER), set(protocol.BUTTON_NAMES))
        self.assertEqual(set(self.module.BUTTON_LABELS), set(protocol.BUTTON_NAMES))

    def test_shows_the_saved_settings(self):
        w = self.window
        self.assertEqual(w.gyro_button.currentData(), "TURBO")
        self.assertEqual(w.gyro_ratchet.currentData(), "M2")
        self.assertEqual(w.horizontal_scale.value(), 1.25)
        self.assertEqual(w.remap_rows["M1"].combo.currentData(), self.module.KEY)
        self.assertEqual(w.remap_rows["M1"].edit.text(), "space")
        self.assertEqual(w.remap_rows["C"].combo.currentData(), "mouse:right")
        self.assertFalse(w.save_button.isEnabled())  # nothing changed yet
        self.assertEqual(w.remap_rows["M2"].note.text(), "used for gyro pause")
        self.assertFalse(w.remap_rows["M2"].combo.isEnabled())

    def test_edit_and_save(self):
        w = self.window
        w.sensitivity.setValue(22.5)
        w.invert_y.setChecked(True)
        w.left_deadzone.setValue(0.1)
        z = w.remap_rows["Z"]
        z.combo.setCurrentIndex(z.combo.findData(self.module.KEY))
        z.edit.setText("ctrl+c")
        w.remap_rows["RM"].combo.setCurrentIndex(w.remap_rows["RM"].combo.findData("NONE"))
        self.assertTrue(w.save_button.isEnabled())
        self.assertTrue(w.save())

        saved = config.load(self.path)
        self.assertEqual(saved.gyro.sensitivity, 22.5)
        self.assertTrue(saved.gyro.invert_y)
        self.assertEqual(saved.left_deadzone, 0.1)
        self.assertEqual(saved.key_remap["Z"].text, "key:ctrl+c")
        self.assertEqual(saved.key_remap["M1"].text, "key:space")  # untouched settings are kept
        self.assertEqual(saved.remap["RM"], "NONE")
        self.assertTrue(os.path.exists(self.path + ".bak"))
        self.assertFalse(w.save_button.isEnabled())

    def test_mistakes_block_saving(self):
        w = self.window
        z = w.remap_rows["Z"]
        z.combo.setCurrentIndex(z.combo.findData(self.module.KEY))
        z.edit.setText("banana")
        self.assertFalse(w.save_button.isEnabled())
        self.assertIn("unknown key 'banana'", w.message.text())
        z.edit.setText("f13")
        self.assertTrue(w.save_button.isEnabled())

    def test_ratchet_cannot_match_the_on_off_button(self):
        w = self.window
        w.gyro_button.setCurrentIndex(w.gyro_button.findData("M2"))
        self.assertFalse(w.save_button.isEnabled())
        self.assertIn("can't be the same button", w.message.text())

    def test_revert_and_defaults(self):
        w = self.window
        w.sensitivity.setValue(40)
        w.revert()
        self.assertEqual(w.sensitivity.value(), 15.0)
        w.restore_defaults()
        self.assertEqual(w.gyro_ratchet.currentData(), "NONE")
        self.assertEqual(w.remap_rows["M1"].combo.currentData(), "")
        self.assertTrue(w.save_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
