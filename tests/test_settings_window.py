import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no screen needed

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # the settings window is optional
    QApplication = None

from vader5 import config, launch, protocol  # noqa: E402


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

    def test_controller_summary(self):
        summary = self.module.controller_summary
        self.assertEqual(summary(None, True), "Connected")
        self.assertEqual(summary(None, False), self.module.NOT_CONNECTED)
        self.assertEqual(summary({"connected": False}, False), self.module.NOT_CONNECTED)
        self.assertEqual(summary({"connected": True, "connection": "wireless", "battery": "80%",
                                  "battery_percent": 80, "gyro": True}, False),
                         "Connected (wireless) · battery 80% · gyro aiming on")
        self.assertIn("profile Street Fighter 6", summary({"connected": True, "profile": "Street Fighter 6"}, False))

    def test_firmware_line(self):
        text = self.module.firmware_text
        self.assertEqual(text("7.1.4.0", True), ("Firmware: 7.1.4.0 · tested with this app", False))
        self.assertEqual(text("7.1.4.0", False)[0], "Firmware: 7.1.4.0 (last connected controller) · tested with this app")
        newer, warning = text("7.1.4.1", True)
        self.assertTrue(warning)
        self.assertIn("take over the controller", newer)
        self.assertFalse(text(None, False)[1])
        self.assertEqual(self.window.windowTitle(), "The Tux InVader")
        self.assertTrue(self.window.firmware_label.text().startswith("Firmware:"))

    def test_opening_the_app_starts_the_tray_icon(self):
        calls = []
        self.assertTrue(self.module.start_tray(spawn=lambda command, **options: calls.append((command, options))))
        self.assertEqual(calls[0][0], launch.command("tray"))
        self.assertTrue(calls[0][1]["start_new_session"])  # keeps running after the window closes

        def missing(command, **options):
            raise FileNotFoundError(command[0])
        self.assertFalse(self.module.start_tray(spawn=missing))

    def test_second_launch_brings_the_open_window_back(self):
        path = os.path.join(self.folder.name, "settings.sock")
        instance = self.module.SingleInstance(self.window, path)
        self.window.hide()
        self.assertTrue(self.module.forward_to_running_window(path, wait_s=1.0))
        for _ in range(50):
            self.app.processEvents()
            if self.window.isVisible():
                break
            time.sleep(0.02)
        self.assertTrue(self.window.isVisible())
        instance.close()
        self.assertFalse(self.module.forward_to_running_window(path, wait_s=0.2))  # nothing open any more

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
        w.gyro_space.setCurrentIndex(w.gyro_space.findData("player"))
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
        self.assertEqual(saved.gyro.space, "player")
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

    def test_game_profiles(self):
        w = self.window
        self.assertIsNone(w.add_profile("Street Fighter 6", steam_app_id=1364780))
        self.assertEqual(w.current, 0)
        self.assertEqual(w.sensitivity.value(), 15.0)  # a new profile starts out like the main settings
        self.assertTrue(w.delete_profile_button.isEnabled())
        w.sensitivity.setValue(25.0)
        w.remap_rows["M1"].combo.setCurrentIndex(0)  # M1 back to itself in this game

        w.profile_select.setCurrentIndex(0)  # back to the main settings
        self.assertEqual(w.current, -1)
        self.assertEqual(w.sensitivity.value(), 15.0)
        self.assertEqual(w.remap_rows["M1"].edit.text(), "space")
        self.assertIsNotNone(w.add_profile("street fighter 6", process="sf6.exe"))  # name already used
        self.assertIsNotNone(w.add_profile("Other"))  # no game chosen

        self.assertTrue(w.save())
        saved = config.load(self.path)
        self.assertEqual(len(saved.profiles), 1)
        profile = saved.profiles[0]
        self.assertEqual((profile.name, profile.steam_app_ids), ("Street Fighter 6", (1364780,)))
        self.assertEqual(profile.overrides, {"gyro": {"sensitivity": 25.0}, "remap": {"M1": "M1"}})
        self.assertEqual(saved.for_profile("Street Fighter 6").gyro.horizontal_scale, 1.25)  # follows main

        w.profile_select.setCurrentIndex(1)
        self.assertEqual(w.sensitivity.value(), 25.0)
        w.restore_defaults()  # "Match main settings"
        self.assertEqual(w.sensitivity.value(), 15.0)
        w.delete_profile(confirm=False)
        self.assertEqual(w.current, -1)
        self.assertTrue(w.save())
        self.assertEqual(config.load(self.path).profiles, [])

    def test_led_tab(self):
        w = self.window
        self.assertEqual(w.led_effect.currentData(), "controller")
        self.assertFalse(w.led_colors.isEnabled())
        self.assertFalse(w.save_button.isEnabled())

        w.led_effect.setCurrentIndex(w.led_effect.findData("breath"))
        self.assertTrue(w.led_colors.isEnabled())
        self.assertTrue(w.led_speed.isEnabled())
        w.led_colors.set_colors([(255, 0, 0), (0, 0, 255)])
        w.led_brightness.setValue(80)
        w.led_speed.setValue(7)
        self.assertTrue(w.save())
        saved = config.load(self.path).led
        self.assertEqual((saved.effect, saved.colors, saved.brightness, saved.speed),
                         ("breath", ((255, 0, 0), (0, 0, 255)), 80, 7))

        w.led_effect.setCurrentIndex(w.led_effect.findData("static"))  # one color: the extra one is dropped
        self.assertEqual(w.led_colors.colors(), ((255, 0, 0),))
        self.assertFalse(w.led_speed.isEnabled())
        self.assertIn("steady", w.led_effect_hint.text())
        w.led_effect.setCurrentIndex(w.led_effect.findData("press_flash"))
        self.assertIn("background service", w.led_effect_hint.text())
        w.led_colors.set_colors([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)])
        self.assertFalse(w.led_colors.add_button.isEnabled())  # 4 is the most for this effect
        self.assertTrue(w._led_form.isRowVisible(w.led_specific))
        self.assertFalse(w._led_form.isRowVisible(w.led_button_colors))
        w.led_specific.setChecked(True)
        self.assertTrue(w._led_form.isRowVisible(w.led_button_colors))
        self.assertFalse(w.led_colors.isEnabled())  # the normal colors aren't used
        w.led_button_colors.set_color("LB", (255, 0, 0))
        w.led_button_colors.set_color("A", (0, 255, 0))
        w.led_button_colors.button.setCurrentIndex(w.led_button_colors.button.findData("A"))
        self.assertTrue(w.led_button_colors.clear_button.isEnabled())
        self.assertIn("LB", w.led_button_colors.summary.text())
        self.assertTrue(w.save())
        saved = config.load(self.path).led
        self.assertTrue(saved.specific_buttons)
        self.assertEqual(dict(saved.button_colors), {"A": (0, 255, 0), "LB": (255, 0, 0)})
        w.led_button_colors.set_color("LB", None)  # "No flash"
        self.assertEqual(w.led_button_colors.button_colors(), (("A", (0, 255, 0)),))
        w.led_effect.setCurrentIndex(w.led_effect.findData("strobe"))
        self.assertIn("Experimental", w.led_effect_hint.text())
        self.assertFalse(w._led_form.isRowVisible(w.led_specific))  # only for Flash on button press
        self.assertTrue(w.led_colors.isEnabled())

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
