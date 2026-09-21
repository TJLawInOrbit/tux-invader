"""Every part of The Tux InVader behind one command.

    python3 -m vader5 [command] [options]      (the AppImage runs this too)

Without a command it opens the settings window. The vader5-* scripts in the project folder start the
same parts directly.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

from . import APP_NAME, TAGLINE, VERSION

PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_DIR = os.path.dirname(PACKAGE_DIR)
DATA_DIR = os.path.join(PACKAGE_DIR, "data")
ICON_FILE = os.path.join(DATA_DIR, "tux-invader.svg")

# command -> (module, function, script in the project folder, help)
PARTS = {
    "settings": ("vader5.settings_window", "main", "vader5-settings", "open the settings window and the tray icon (the default)"),
    "tray": ("vader5.tray", "main", "vader5-tray", "show the tray icon"),
    "pad": ("vader5.pad", "main", "vader5-pad", "run the virtual controller (what the background service runs)"),
    "viewer": ("vader5.viewer", "main", "vader5-viewer", "live view of everything the controller sends"),
    "config": ("vader5.config", "main", "vader5-config", "create or check the settings file"),
    "setup": ("vader5.install", "setup_main", None, "one-time setup: permissions, background service, app menu"),
    "uninstall": ("vader5.install", "uninstall_main", None, "remove what setup added (your settings are kept)"),
}


def appimage() -> str | None:
    """The AppImage file this is running from, if any."""
    return os.environ.get("APPIMAGE") or None


def command(part: str, appimage_path: str | None = None) -> list[str]:
    """The command line that starts one part of the app as its own process."""
    path = appimage_path or appimage()
    if path:
        return [path, part]
    script = PARTS[part][2]
    if script:
        return [os.path.join(PROJECT_DIR, script)]
    return [sys.executable, "-m", "vader5", part]


def start_detached(argv: list[str], spawn=subprocess.Popen, env: dict | None = None) -> bool:
    """Start a process that keeps running on its own. False if it couldn't be started.

    It's started through a shell that exits at once, so the process isn't this one's child and doesn't
    linger as a "defunct" entry after it finishes."""
    try:
        shell = spawn(["/bin/sh", "-c", '"$@" &', "sh", *argv], stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    except OSError:
        return False
    try:
        shell.wait(timeout=5)
    except (AttributeError, subprocess.TimeoutExpired):
        pass
    return True


def usage() -> str:
    lines = [f"{APP_NAME} {VERSION}: {TAGLINE}", "", "Usage: [command] [options]", "", "Commands:"]
    lines += [f"  {name:<10} {text}" for name, (_, _, _, text) in PARTS.items()]
    lines += ["", "Add --help after a command to see its options."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    if args and args[0] in ("-V", "--version", "version"):
        print(f"{APP_NAME} {VERSION}")
        return 0
    name = args.pop(0) if args else "settings"
    if name not in PARTS:
        print(f"Unknown command {name!r}.\n\n{usage()}", file=sys.stderr)
        return 2
    module, function, _, _ = PARTS[name]
    sys.argv[0] = name  # so a command's help says "usage: config ...", not "usage: __main__.py ..."
    return getattr(importlib.import_module(module), function)(args) or 0
