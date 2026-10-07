import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from vader5 import VERSION, install, launch  # noqa: E402


class FakeRunner:
    def __init__(self, fail=()):
        self.calls, self.inputs, self.fail = [], [], fail

    def __call__(self, argv, text_in=None):
        self.calls.append(argv)
        self.inputs.append(text_in)
        code = 126 if argv[0] in self.fail else 0
        return subprocess.CompletedProcess(argv, code, "", "cancelled" if code else "")

    def ran(self, *start):
        return any(call[:len(start)] == list(start) for call in self.calls)

    def input_to(self, program):
        """What was fed to that program's input, if it was run."""
        for argv, text in zip(self.calls, self.inputs):
            if argv[0] == program:
                return text
        return None


class LaunchTests(unittest.TestCase):
    def test_commands(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": ""}):
            self.assertEqual(launch.command("pad"), [os.path.join(launch.PROJECT_DIR, "vader5-pad")])
            self.assertEqual(launch.command("setup"), [sys.executable, "-m", "vader5", "setup"])
        self.assertEqual(launch.command("tray", "/home/me/Applications/app.AppImage"),
                         ["/home/me/Applications/app.AppImage", "tray"])
        for part in launch.PARTS:
            script = launch.PARTS[part][2]
            if script:
                self.assertTrue(os.path.exists(os.path.join(launch.PROJECT_DIR, script)), script)

    def test_started_processes_are_not_left_behind(self):
        import time
        marker = os.path.join(tempfile.mkdtemp(), "ran")
        self.assertTrue(launch.start_detached(["/bin/sh", "-c", f'sleep 0.3; touch "{marker}"']))
        for _ in range(40):
            if os.path.exists(marker):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(marker))  # it ran, on its own
        time.sleep(0.1)
        with self.assertRaises(ChildProcessError):  # and it isn't this process's child, left behind as "defunct"
            os.waitpid(-1, os.WNOHANG)

    def test_a_missing_program_is_reported(self):
        def missing(*args, **options):
            raise FileNotFoundError(args[0][0])
        self.assertFalse(launch.start_detached(["no-such-program"], spawn=missing))

    def test_version_help_and_unknown_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(launch.main(["--version"]), 0)
            self.assertEqual(launch.main(["--help"]), 0)
        self.assertIn(f"The Tux InVader {VERSION}", out.getvalue())
        self.assertIn("setup", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(launch.main(["dance"]), 2)
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text), mock.patch.object(sys, "argv", ["__main__.py"]):
            with self.assertRaises(SystemExit):
                launch.main(["config", "--help"])
        self.assertIn("usage: config", help_text.getvalue())

    def test_the_license_is_there_and_named_in_the_version(self):
        with open(os.path.join(launch.PROJECT_DIR, "LICENSE")) as file:
            self.assertIn("GNU GENERAL PUBLIC LICENSE", file.read(200))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            launch.main(["--version"])
        self.assertIn("GNU GPL version 3", out.getvalue())

    def test_icon_and_rule_are_in_the_package(self):
        self.assertTrue(os.path.exists(launch.ICON_FILE))
        rule = install.rule_text()
        for needed in ('ATTRS{idVendor}=="37d7"', 'SUBSYSTEM=="usb"', 'KERNEL=="uinput"'):
            self.assertIn(needed, rule)


class QuotingTests(unittest.TestCase):
    def test_desktop_exec(self):
        self.assertEqual(install.desktop_exec(["/home/me/Games & Apps/app.AppImage", "tray"]),
                         '"/home/me/Games & Apps/app.AppImage" "tray"')
        self.assertEqual(install.desktop_exec(['/a/100%/$x"y']), '"/a/100%%/\\$x\\"y"')

    def test_unit_exec(self):
        self.assertEqual(install.unit_exec(["/a/100%/$HOME/b c", "pad"]), '"/a/100%%/$$HOME/b c" "pad"')


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.folder.name, "home")
        os.makedirs(self.home)
        self.env = {"HOME": self.home}
        self.places = install.Places.for_user(self.env)
        self.spawned, self.stopped = [], []
        patcher = mock.patch.object(install, "RULE_TARGET", os.path.join(self.folder.name, "rules.d", "70.rules"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.folder.cleanup)

    def setup(self, env, runner, parts=install.SETUP_PARTS):
        return install.setup(parts, env=env, runner=runner, spawn=self.spawned.append,
                             stop=lambda: self.stopped.append(True))

    def test_setup_from_an_appimage(self):
        download = os.path.join(self.folder.name, "Downloads", "The_Tux_InVader-0.4-x86_64.AppImage")
        os.makedirs(os.path.dirname(download))
        with open(download, "wb") as file:
            file.write(b"appimage")
        old_copy = os.path.join(self.home, "Applications", "The_Tuxedo_InVader.AppImage")  # under the earlier name
        old_icon = os.path.join(os.path.dirname(self.places.icon_file), "tuxedo-invader.svg")
        for path in (old_copy, old_icon):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        runner = FakeRunner()
        messages = self.setup({**self.env, "APPIMAGE": download}, runner)

        copy = os.path.join(self.home, "Applications", install.APPIMAGE_NAME)
        with open(copy, "rb") as file:
            self.assertEqual(file.read(), b"appimage")
        self.assertTrue(os.access(copy, os.X_OK))
        with open(self.places.unit_file) as file:
            self.assertIn(f'ExecStart="{copy}" "pad"', file.read())
        with open(self.places.menu_entry) as file:
            entry = file.read()
        self.assertIn(f'Exec="{copy}" "settings"', entry)
        self.assertIn("Categories=Game;Utility;", entry)
        self.assertIn("Icon=tux-invader", entry)
        with open(self.places.tray_autostart) as file:
            self.assertIn(f'Exec="{copy}" "tray"', file.read())
        self.assertTrue(os.path.exists(self.places.icon_file))
        self.assertFalse(os.path.exists(old_copy) or os.path.exists(old_icon))  # leftovers from the earlier name
        self.assertEqual(self.spawned, [[copy, "tray"]])
        self.assertEqual(self.stopped, [True])  # the old tray icon was asked to quit
        self.assertTrue(runner.ran("systemctl", "--user", "enable", install.UNIT))
        self.assertTrue(runner.ran("pkexec"))
        # the rule reaches root as input, with no file anything else could swap first
        self.assertEqual(runner.input_to("pkexec"), install.rule_text())
        self.assertNotIn(self.places.staged_rule, " ".join(runner.calls[-1]))
        self.assertFalse(os.path.exists(self.places.staged_rule))
        self.assertIn("permissions rule is installed", " ".join(messages))

    def test_setup_from_the_project_folder_without_the_rule(self):
        runner = FakeRunner()
        with mock.patch.dict(os.environ, {"APPIMAGE": ""}):
            self.setup(self.env, runner, parts=("service", "menu"))
        with open(self.places.unit_file) as file:
            self.assertIn(os.path.join(launch.PROJECT_DIR, "vader5-pad"), file.read())
        self.assertFalse(runner.ran("pkexec"))
        self.assertFalse(os.path.exists(os.path.join(self.home, "Applications")))

    def test_rule_already_installed_or_cancelled(self):
        os.makedirs(os.path.dirname(install.RULE_TARGET))
        with open(install.RULE_TARGET, "w") as file:
            file.write("an older rule without uinput\n")
        cancelled = self.setup(self.env, FakeRunner(fail=("pkexec",)), parts=("rule",))
        self.assertIn("sudo install", cancelled[0])
        with open(self.places.staged_rule) as file:  # only then is a copy left to install by hand
            self.assertEqual(file.read(), install.rule_text())
        with open(install.RULE_TARGET, "w") as file:
            file.write(install.rule_text())
        runner = FakeRunner()
        self.assertIn("already installed", self.setup(self.env, runner, parts=("rule",))[0])
        self.assertFalse(runner.ran("pkexec"))

    def test_replaces_an_older_copy(self):
        new = os.path.join(self.folder.name, "new.AppImage")
        with open(new, "wb") as file:
            file.write(b"new")
        applications = os.path.join(self.home, "Applications")
        os.makedirs(applications)
        with open(os.path.join(applications, install.APPIMAGE_NAME), "wb") as file:
            file.write(b"old")
        copy = install.install_appimage(new, applications)
        with open(copy, "rb") as file:
            self.assertEqual(file.read(), b"new")
        self.assertEqual(install.install_appimage(copy, applications), copy)  # running the copy itself

    def test_uninstall_keeps_settings(self):
        settings = os.path.join(self.home, ".config", "vader5", "config.toml")
        os.makedirs(os.path.dirname(settings))
        with open(settings, "w") as file:
            file.write("[gyro]\n")
        download = os.path.join(self.folder.name, "app.AppImage")
        with open(download, "wb") as file:
            file.write(b"appimage")
        self.setup({**self.env, "APPIMAGE": download}, FakeRunner(), parts=("service", "menu"))
        self.assertTrue(install.is_set_up(self.env))

        runner = FakeRunner()
        install.uninstall(("service", "menu", "app"), env=self.env, runner=runner, stop=lambda: self.stopped.append(True))
        for path in (self.places.unit_file, self.places.menu_entry, self.places.tray_autostart, self.places.icon_file,
                     os.path.join(self.home, "Applications", install.APPIMAGE_NAME)):
            self.assertFalse(os.path.exists(path), path)
        self.assertTrue(os.path.exists(settings))
        self.assertTrue(runner.ran("systemctl", "--user", "disable", "--now", install.UNIT))
        self.assertFalse(runner.ran("pkexec"))  # the rule stays unless asked
        self.assertFalse(install.is_set_up(self.env))


if __name__ == "__main__":
    unittest.main()
