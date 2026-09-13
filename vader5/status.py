"""A small status file that vader5-pad keeps up to date for the tray icon and the settings window.

It lives in $XDG_RUNTIME_DIR/vader5/status.json (a temporary folder for this login), is rewritten on
every change and at least every few seconds, and is removed when vader5-pad stops. Readers never talk
to the controller, so they can't disturb it.
"""

from __future__ import annotations

import fcntl
import json
import os
import time

STALE_S = 15.0  # older than this means vader5-pad isn't running any more


def runtime_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/vader5-{os.getuid()}"
    return os.path.join(base, "vader5")


def status_path() -> str:
    return os.path.join(runtime_dir(), "status.json")


def write(status: dict, path: str | None = None) -> None:
    path = path or status_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump({**status, "updated": time.time()}, f)
    os.replace(temporary, path)  # readers never see a half-written file


def remove(path: str | None = None) -> None:
    try:
        os.remove(path or status_path())
    except FileNotFoundError:
        pass


def read(path: str | None = None, now: float | None = None) -> dict | None:
    """The latest status, or None if vader5-pad isn't running (no file, or not updated recently)."""
    try:
        with open(path or status_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    now = time.time() if now is None else now
    if not isinstance(data, dict) or now - data.get("updated", 0) > STALE_S:
        return None
    return data


def battery_text(status: dict) -> str | None:
    """Battery for display, like "80%", "60%, charging" or "full"; None if unknown."""
    if status.get("battery") == "full":
        return "full"
    percent = status.get("battery_percent")
    if percent is None:
        return None
    return f"{percent}%, charging" if status.get("charging") else f"{percent}%"


def single_instance_lock(name: str):
    """A lock held for as long as this process runs; None if another process already holds it."""
    os.makedirs(runtime_dir(), exist_ok=True)
    lock = open(os.path.join(runtime_dir(), f"{name}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock
