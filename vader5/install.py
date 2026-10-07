"""One-time setup for The Tux InVader, and removing it again.

    python3 -m vader5 setup [--only service|menu|rule] [--no-rule]
    python3 -m vader5 uninstall [--only service|menu|rule|app] [--remove-rule] [--remove-app]

Setup does what the app needs to run by itself:
- from an AppImage: copies it to ~/Applications, so moving or deleting the downloaded file breaks nothing
- the permissions rule in /etc/udev/rules.d (asks for your password once): the controller, its USB device
  (to hide the basic Xbox pad) and /dev/uinput (the virtual controller, keyboard and mouse)
- the background service: a systemd user service that starts at login
- the app menu entry with its icon, and the tray icon at login
Nothing else on the system is changed. Uninstall removes it again; your settings are kept.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass

from . import APP_NAME, TAGLINE
from . import status as status_file
from .launch import DATA_DIR, ICON_FILE, command, start_detached

UNIT = "vader5-pad.service"
RULE_NAME = "70-vader5-pro.rules"
RULE_SOURCE = os.path.join(DATA_DIR, RULE_NAME)
RULE_TARGET = "/etc/udev/rules.d/" + RULE_NAME
ICON_NAME = "tux-invader"
OLD_ICON_NAMES = ("tuxedo-invader",)
APPIMAGE_NAME = "The_Tux_InVader.AppImage"
OLD_APPIMAGE_NAMES = ("The_Tuxedo_InVader.AppImage",)  # the app was called The Tuxedo InVader at first
SETUP_PARTS = ("service", "menu", "rule")
TRIGGER = ("udevadm control --reload-rules"
           " && udevadm trigger --action=change --subsystem-match=hidraw"
           " && udevadm trigger --action=change --subsystem-match=misc --sysname-match=uinput"
           " && udevadm trigger --action=change --subsystem-match=usb --attr-match=idVendor=37d7")


@dataclass(frozen=True)
class Places:
    """Where setup puts things for one user."""

    unit_file: str
    menu_entry: str
    old_tray_entry: str
    tray_autostart: str
    icon_file: str
    applications: str
    staged_rule: str  # a copy of the rule that root can read (an AppImage's files can't be read by root)

    @classmethod
    def for_user(cls, env=None) -> Places:
        env = os.environ if env is None else env
        home = env.get("HOME") or os.path.expanduser("~")
        config = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        data = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        return cls(
            unit_file=os.path.join(config, "systemd", "user", UNIT),
            menu_entry=os.path.join(data, "applications", "vader5-settings.desktop"),
            old_tray_entry=os.path.join(data, "applications", "vader5-tray.desktop"),
            tray_autostart=os.path.join(config, "autostart", "vader5-tray.desktop"),
            icon_file=os.path.join(data, "icons", "hicolor", "scalable", "apps", ICON_NAME + ".svg"),
            applications=os.path.join(home, "Applications"),
            staged_rule=os.path.join(config, "vader5", RULE_NAME),
        )


def desktop_exec(argv: list[str]) -> str:
    """An Exec= line: every argument quoted, as the desktop entry spec asks for paths with spaces."""
    def quote(arg: str) -> str:
        arg = arg.replace("\\", "\\\\\\\\")
        arg = re.sub(r'([`"$])', r"\\\1", arg)
        return '"' + arg.replace("%", "%%") + '"'
    return " ".join(quote(arg) for arg in argv)


def unit_exec(argv: list[str]) -> str:
    """An ExecStart= line: quoted arguments, with systemd's % and $ doubled."""
    def quote(arg: str) -> str:
        arg = arg.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + arg.replace("%", "%%").replace("$", "$$") + '"'
    return " ".join(quote(arg) for arg in argv)


def unit_text(pad_command: list[str]) -> str:
    return f"""[Unit]
Description={APP_NAME}: Flydigi Vader 5 Pro as a virtual Xbox Elite controller

[Service]
ExecStart={unit_exec(pad_command)}
Restart=on-failure
RestartSec=3
# 143/129 = stopped by SIGTERM/SIGHUP after cleaning up, which is a normal stop.
SuccessExitStatus=143 129

[Install]
WantedBy=default.target
"""


def menu_entry_text(settings_command: list[str]) -> str:
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
GenericName=Controller Settings
Comment={TAGLINE}
Exec={desktop_exec(settings_command)}
Icon={ICON_NAME}
Terminal=false
Categories=Game;Utility;
Keywords=tux;invader;flydigi;vader;controller;gamepad;gyro;
StartupWMClass=vader5-settings
"""


def tray_entry_text(tray_command: list[str]) -> str:
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Comment=Battery and status of the Flydigi Vader 5 Pro in the system tray
Exec={desktop_exec(tray_command)}
Icon={ICON_NAME}
Terminal=false
"""


def rule_text() -> str:
    with open(RULE_SOURCE) as file:
        return file.read()


def rule_installed(target: str | None = None) -> bool:
    """The current permissions rule is in place (an older copy without everything counts as missing)."""
    try:
        with open(target or RULE_TARGET) as file:
            return file.read() == rule_text()
    except OSError:
        return False


def is_set_up(env=None) -> bool:
    places = Places.for_user(env)
    return os.path.exists(places.unit_file) and os.path.exists(places.menu_entry)


def run(argv: list[str], text_in: str | None = None) -> subprocess.CompletedProcess:
    """Run a command, reporting a missing program as a failure instead of an exception."""
    try:
        return subprocess.run(argv, input=text_in, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 127, "", str(error))


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".new"
    with open(temporary, "w") as file:
        file.write(text)
    os.replace(temporary, path)


def _remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def install_appimage(source: str, applications: str) -> str:
    """Copy the AppImage to ~/Applications (replacing an older copy) and return where it is."""
    target = os.path.join(applications, APPIMAGE_NAME)
    if os.path.exists(target) and os.path.samefile(source, target):
        return target
    os.makedirs(applications, exist_ok=True)
    temporary = target + ".new"
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o755)
    os.replace(temporary, target)  # a copy that's still running keeps working
    return target


def stop_tray() -> bool:
    """Ask the running tray icon to quit. It's found by its lock, so nothing else with a similar name is touched."""
    pid = status_file.lock_holder("vader5-tray")
    if pid is None or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def setup(parts=SETUP_PARTS, env=None, runner=run, spawn=start_detached, stop=stop_tray) -> list[str]:
    """Set up the chosen parts. Returns what was done, for the user to read."""
    env = os.environ if env is None else env
    places = Places.for_user(env)
    messages = []
    appimage = env.get("APPIMAGE") or None
    if appimage and {"service", "menu"} & set(parts):
        appimage = install_appimage(appimage, places.applications)
        messages.append(f"Copied the app to {appimage}.")
        for old in OLD_APPIMAGE_NAMES:  # a copy saved under the app's earlier name
            _remove(os.path.join(places.applications, old))

    if "rule" in parts:
        messages.append(_install_rule(places, runner))

    if "service" in parts:
        _write(places.unit_file, unit_text(command("pad", appimage)))
        runner(["systemctl", "--user", "daemon-reload"])
        runner(["systemctl", "--user", "enable", UNIT])
        result = runner(["systemctl", "--user", "restart", UNIT])
        messages.append("The background service is installed and running, and starts every time you log in."
                        if result.returncode == 0 else
                        f"The background service is installed but didn't start: {result.stderr.strip()}")

    if "menu" in parts:
        with open(ICON_FILE) as file:
            _write(places.icon_file, file.read())
        for old in OLD_ICON_NAMES:
            _remove(os.path.join(os.path.dirname(places.icon_file), old + ".svg"))
        # icon caches (KDE's among them) notice a changed icon by the theme folder's timestamp
        os.utime(os.path.dirname(os.path.dirname(os.path.dirname(places.icon_file))))
        _write(places.menu_entry, menu_entry_text(command("settings", appimage)))
        _remove(places.old_tray_entry)  # older versions had a second menu entry just for the tray icon
        _write(places.tray_autostart, tray_entry_text(command("tray", appimage)))
        runner(["update-desktop-database", os.path.dirname(places.menu_entry)])
        runner(["kbuildsycoca6"])  # KDE's menu cache; other desktops don't have it, which is fine
        stop()  # a tray icon running from an older location
        spawn(command("tray", appimage))  # waits for the old one to close, or quits if one is already running
        messages.append(f'"{APP_NAME}" is in your app menu (Games and Utilities), and the tray icon starts at login.')
    return messages


def _install_rule(places: Places, runner) -> str:
    if rule_installed():
        return "The permissions rule is already installed."
    # The rule is handed to root through the command's input, not as a file: a file in the user's own
    # folder could be swapped for another one between writing it and root reading it, and a udev rule
    # can run programs as root.
    result = runner(["pkexec", "/bin/sh", "-c", f"cat > {RULE_TARGET} && chmod 644 {RULE_TARGET} && {TRIGGER}"],
                    rule_text())
    if result.returncode == 0:
        return ("The permissions rule is installed. If the controller doesn't respond, unplug it (or the "
                "dongle) and plug it back in, and press the Home button.")
    _write(places.staged_rule, rule_text())  # a copy to install by hand, since the prompt didn't work
    return ("The permissions rule wasn't installed (the password prompt was cancelled or isn't available). "
            f"To install it by hand, run:\n  sudo install -m 644 '{places.staged_rule}' {RULE_TARGET}"
            " && sudo udevadm control --reload-rules\nthen unplug the controller and plug it back in, and press the Home button.")


def uninstall(parts=("service", "menu"), env=None, runner=run, stop=stop_tray) -> list[str]:
    """Remove the chosen parts: service, menu, rule, app. Settings in ~/.config/vader5 are always kept."""
    places = Places.for_user(env)
    messages = []
    if "service" in parts:
        runner(["systemctl", "--user", "disable", "--now", UNIT])
        _remove(places.unit_file)
        runner(["systemctl", "--user", "daemon-reload"])
        messages.append("Removed the background service; the basic Xbox pad is back.")
    if "menu" in parts:
        for path in (places.menu_entry, places.old_tray_entry, places.tray_autostart, places.icon_file):
            _remove(path)
        for old in OLD_ICON_NAMES:
            _remove(os.path.join(os.path.dirname(places.icon_file), old + ".svg"))
        runner(["update-desktop-database", os.path.dirname(places.menu_entry)])
        stop()
        messages.append("Removed the app menu entry and the tray icon.")
    if "rule" in parts:
        result = runner(["pkexec", "/bin/sh", "-c", f"rm -f {RULE_TARGET} && udevadm control --reload-rules"])
        _remove(places.staged_rule)
        messages.append("Removed the permissions rule." if result.returncode == 0 else
                        f"The permissions rule wasn't removed. To remove it by hand: sudo rm {RULE_TARGET}")
    if "app" in parts:
        for old in OLD_APPIMAGE_NAMES:
            _remove(os.path.join(places.applications, old))
        removed = _remove(os.path.join(places.applications, APPIMAGE_NAME))
        messages.append(f"Removed {os.path.join(places.applications, APPIMAGE_NAME)}." if removed else
                        "There was no app copy in ~/Applications to remove.")
    messages.append("Your settings are kept in ~/.config/vader5.")
    return messages


def setup_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup", description=f"One-time setup for {APP_NAME}.")
    parser.add_argument("--only", action="append", choices=SETUP_PARTS,
                        help="set up only this part (can be given more than once)")
    parser.add_argument("--no-rule", action="store_true", help="skip the permissions rule (no password prompt)")
    args = parser.parse_args(argv)
    parts = [part for part in (args.only or SETUP_PARTS) if not (args.no_rule and part == "rule")]
    for message in setup(parts):
        print(message)
    return 0


def uninstall_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uninstall", description=f"Remove what setup added for {APP_NAME}.")
    parser.add_argument("--only", action="append", choices=(*SETUP_PARTS, "app"),
                        help="remove only this part (can be given more than once)")
    parser.add_argument("--remove-rule", action="store_true", help="also remove the permissions rule (asks for your password)")
    parser.add_argument("--remove-app", action="store_true", help="also delete the app copy in ~/Applications")
    args = parser.parse_args(argv)
    parts = args.only or ["service", "menu"] + (["rule"] if args.remove_rule else []) + (["app"] if args.remove_app else [])
    for message in uninstall(parts):
        print(message)
    return 0
