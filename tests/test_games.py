import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from vader5 import config, games  # noqa: E402

PROFILES = config.parse("""
    [[profile]]
    name = "Street Fighter 6"
    steam_app_id = 1364780

    [[profile]]
    name = "Retro"
    process = "retroarch"
""").profiles


def write(path, content, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(content)


class SteamLibraryTests(unittest.TestCase):
    def test_installed_games_from_every_library(self):
        with tempfile.TemporaryDirectory() as folder:
            root, games_drive = os.path.join(folder, "Steam"), os.path.join(folder, "Games")
            write(os.path.join(root, "steamapps", "libraryfolders.vdf"),
                  f'"libraryfolders"\n{{\n "0" {{ "path" "{root}" }}\n "1" {{ "path" "{games_drive}" }}\n'
                  f' "2" {{ "path" "{folder}/unplugged" }}\n}}\n')
            write(os.path.join(root, "steamapps", "appmanifest_228980.acf"),
                  '"AppState" { "appid" "228980" "name" "Steamworks Common Redistributables" }')
            write(os.path.join(games_drive, "steamapps", "appmanifest_1364780.acf"),
                  '"AppState"\n{\n\t"appid"\t\t"1364780"\n\t"name"\t\t"Street Fighter™ 6"\n}')
            write(os.path.join(games_drive, "steamapps", "appmanifest_2379780.acf"),
                  '"AppState" { "appid" "2379780" "name" "Balatro" }')
            write(os.path.join(games_drive, "steamapps", "appmanifest_1493710.acf"),
                  '"AppState" { "appid" "1493710" "name" "Proton Experimental" }')
            self.assertEqual(games.steam_libraries((root,)), [root, games_drive])
            self.assertEqual(games.installed_steam_games((root,)), [(2379780, "Balatro"), (1364780, "Street Fighter™ 6")])

    def test_no_steam(self):
        self.assertEqual(games.installed_steam_games(("/nonexistent/Steam",)), [])


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.proc = self.folder.name

    def tearDown(self):
        self.folder.cleanup()

    def add_process(self, pid, started, comm, cmdline=(), env=()):
        base = os.path.join(self.proc, str(pid))
        write(os.path.join(base, "stat"), f"{pid} ({comm}) S " + "0 " * 18 + f"{started} 0 0\n")
        write(os.path.join(base, "comm"), comm + "\n")
        write(os.path.join(base, "cmdline"), b"\0".join(a.encode() for a in cmdline) + b"\0", "wb")
        write(os.path.join(base, "environ"), b"\0".join(e.encode() for e in env) + b"\0", "wb")

    def remove_process(self, pid):
        base = os.path.join(self.proc, str(pid))
        for name in os.listdir(base):
            os.remove(os.path.join(base, name))
        os.rmdir(base)

    def test_reads_steam_app_id_and_program_names(self):
        self.add_process(100, 5000, "StreetFighter6",
                         ["Z:\\games\\StreetFighter6.exe", "-dx12"], ["HOME=/home/me", "SteamAppId=1364780"])
        self.add_process(200, 6000, "bash", ["/usr/bin/bash"], ["SteamAppId=0"])
        processes = {p.pid: p for p in games.ProcessScanner(self.proc).scan()}
        self.assertEqual(processes[100].steam_app_id, 1364780)
        self.assertIn("streetfighter6.exe", processes[100].names)
        self.assertNotIn("-dx12", processes[100].names)
        self.assertIsNone(processes[200].steam_app_id)  # 0 isn't a game

    def test_most_recently_started_game_wins(self):
        self.add_process(100, 5000, "sf6", env=["SteamAppId=1364780"])
        self.add_process(200, 9000, "retroarch", ["/usr/bin/retroarch"])
        scanner = games.ProcessScanner(self.proc)
        self.assertEqual(games.active_profile(PROFILES, scanner.scan()).name, "Retro")
        self.remove_process(200)
        self.assertEqual(games.active_profile(PROFILES, scanner.scan()).name, "Street Fighter 6")
        self.remove_process(100)
        self.assertIsNone(games.active_profile(PROFILES, scanner.scan()))

    def test_reused_process_id_is_read_again(self):
        self.add_process(100, 5000, "sf6", env=["SteamAppId=1364780"])
        scanner = games.ProcessScanner(self.proc)
        self.assertEqual(scanner.scan()[0].steam_app_id, 1364780)
        self.remove_process(100)
        self.add_process(100, 7000, "bash")
        self.assertIsNone(scanner.scan()[0].steam_app_id)

    def test_watcher_reports_changes_every_couple_of_seconds(self):
        watcher = games.GameWatcher(games.ProcessScanner(self.proc))
        self.assertFalse(watcher.check(0.0, PROFILES))  # nothing running, nothing changed
        self.add_process(100, 5000, "sf6", env=["SteamAppId=1364780"])
        self.assertFalse(watcher.check(1.0, PROFILES))  # not time to look yet
        self.assertTrue(watcher.check(2.0, PROFILES))
        self.assertEqual(watcher.active, "Street Fighter 6")
        self.assertFalse(watcher.check(4.0, PROFILES))
        self.assertTrue(watcher.check(6.0, []))  # profiles removed from the settings
        self.assertIsNone(watcher.active)


if __name__ == "__main__":
    unittest.main()
