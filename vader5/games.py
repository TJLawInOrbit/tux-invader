"""Which game is running, for per-game profiles.

Steam starts every game with SteamAppId=<id> in its environment, and the game's processes
(including Proton and Wine) inherit it, so reading /proc/<pid>/environ finds Steam games. Other
games are matched by program name. Only your own processes are looked at.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

STEAM_ROOTS = ("~/.local/share/Steam", "~/.steam/steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")
_STEAM_TOOLS = re.compile(r"^(Proton\b|Steam Linux Runtime|Steamworks Common Redistributables|SteamVR)", re.IGNORECASE)


def steam_libraries(roots=STEAM_ROOTS) -> list[str]:
    """Every Steam library folder (the ones holding steamapps/), without duplicates."""
    libraries: list[str] = []
    for root in roots:
        vdf = os.path.join(os.path.expanduser(root), "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                paths = re.findall(r'"path"\s+"([^"]+)"', f.read())
        except OSError:
            continue
        for path in paths:
            real = os.path.realpath(path)
            if real not in libraries and os.path.isdir(os.path.join(real, "steamapps")):
                libraries.append(real)
    return libraries


def installed_steam_games(roots=STEAM_ROOTS) -> list[tuple[int, str]]:
    """(app ID, name) of every installed Steam game, sorted by name. Proton and Steam's runtimes are left out."""
    games: dict[int, str] = {}
    for library in steam_libraries(roots):
        for manifest in glob.glob(os.path.join(library, "steamapps", "appmanifest_*.acf")):
            try:
                with open(manifest, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            app_id = re.search(r'"appid"\s+"(\d+)"', text)
            name = re.search(r'"name"\s+"([^"]+)"', text)
            if app_id and name and not _STEAM_TOOLS.search(name.group(1)):
                games[int(app_id.group(1))] = name.group(1)
    return sorted(games.items(), key=lambda game: game[1].lower())


@dataclass(frozen=True)
class RunningProcess:
    pid: int
    started: int  # clock ticks since boot: a bigger number started later
    steam_app_id: int | None
    names: frozenset[str]  # lower-case program names: the process name and file names on its command line


def _read_process(proc: str, pid: str, started: int) -> RunningProcess:
    folder = os.path.join(proc, pid)
    app_id = None
    with open(os.path.join(folder, "environ"), "rb") as f:
        for variable in f.read().split(b"\0"):
            if variable.startswith(b"SteamAppId="):
                value = variable[len(b"SteamAppId="):]
                app_id = int(value) if value.isdigit() and int(value) > 0 else None
                break
    with open(os.path.join(folder, "comm"), encoding="utf-8", errors="replace") as f:
        names = {f.read().strip().lower()}
    with open(os.path.join(folder, "cmdline"), "rb") as f:
        for argument in f.read().split(b"\0"):
            text = argument.decode(errors="replace")
            if text and not text.startswith("-"):
                names.add(os.path.basename(text.replace("\\", "/")).lower())  # Wine uses C:\ paths
    names.discard("")
    return RunningProcess(int(pid), started, app_id, frozenset(names))


def _start_time(proc: str, pid: str) -> int:
    with open(os.path.join(proc, pid, "stat"), "rb") as f:
        return int(f.read().rsplit(b")", 1)[1].split()[19])  # field 22, after the (process name)


class ProcessScanner:
    """Lists your running processes, only reading the details of ones it hasn't seen before."""

    def __init__(self, proc: str = "/proc"):
        self.proc = proc
        self._known: dict[int, RunningProcess] = {}

    def scan(self) -> list[RunningProcess]:
        uid = os.getuid()
        current: dict[int, RunningProcess] = {}
        try:
            entries = list(os.scandir(self.proc))
        except OSError:
            return []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                started = _start_time(self.proc, entry.name)
                known = self._known.get(int(entry.name))
                if known is None or known.started != started:  # new process, or a reused process ID
                    known = _read_process(self.proc, entry.name, started)
                current[known.pid] = known
            except (OSError, ValueError, IndexError):
                continue  # the process ended while we looked
        self._known = current
        return list(current.values())


def active_profile(profiles, processes: list[RunningProcess]):
    """The profile whose game started most recently among the running processes, or None."""
    best, best_started = None, -1
    for process in processes:
        for profile in profiles:
            if process.started > best_started and profile.matches(process):
                best, best_started = profile, process.started
    return best


class GameWatcher:
    """Checks every couple of seconds which profile's game is running."""

    CHECK_S = 2.0

    def __init__(self, scanner: ProcessScanner | None = None):
        self.scanner = scanner or ProcessScanner()
        self.active: str | None = None  # name of the profile in use
        self._next_check = 0.0

    def check(self, now: float, profiles) -> bool:
        """Look again if it's time. Returns True if the profile in use changed."""
        if now < self._next_check:
            return False
        self._next_check = now + self.CHECK_S
        profile = active_profile(profiles, self.scanner.scan()) if profiles else None
        name = profile.name if profile else None
        changed = name != self.active
        self.active = name
        return changed
