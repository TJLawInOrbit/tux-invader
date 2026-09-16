"""Run the Vader 5 Pro as a virtual Xbox Elite controller for games and Steam.

    ./vader5-pad               run until Ctrl+C
    ./vader5-pad --verbose     also print button presses and rumble
    ./vader5-service install   or run it automatically (see README.md)

The virtual controller and the virtual keyboard and mouse only exist while the real controller is
on and sending input, so Steam and games don't list a controller that's switched off. Settings come
from ~/.config/vader5/config.toml (see config.py) and apply as soon as the file is saved. The
current state is written to a status file (see status.py) for the tray icon and settings window.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import sys
import time

import evdev

from . import protocol
from .protocol import firmware_note
from . import status as status_file
from .config import ConfigWatcher, config_path
from .device import Controller, DeviceError, detach_xpad, find_xpad_event, reattach_xpad
from .led import LedSettings, PressFlash, build_blob, geometry
from .led import describe as describe_lights
from .games import GameWatcher
from .gyro import GyroAim
from .keyboard_mouse import VirtualKeyboardMouse
from .status import single_instance_lock
from .virtual_pad import VirtualElite

RECONNECT_S = 1.0
IDLE_S = 3.0  # no input reports for this long: the controller is off or asleep
INFO_REFRESH_S = 30.0  # ask the controller for its battery level this often
RUMBLE_REFRESH_S = 0.5  # re-send active rumble so it never lapses on the controller
GYRO_CUE_ON = ((0, 150), 0.08)  # (strong, weak), seconds: short light buzz = gyro aiming on
GYRO_CUE_OFF = ((150, 0), 0.25)  # longer heavy buzz = gyro aiming off


def log(message: str) -> None:
    print(f"vader5-pad: {message}", flush=True)


def status_fields(pad: Controller | None, active: bool, gyro_on: bool, profile: str | None = None) -> dict:
    """What the tray icon and settings window show about the controller."""
    info = pad.info if pad else None
    return {
        "profile": profile,
        "connected": pad is not None,
        "active": active,
        "connection": info.connection if info else None,
        "firmware": info.firmware if info else None,
        "battery": info.battery if info else None,
        "battery_percent": info.battery_percent if info else None,
        "charging": info.charging if info else False,
        "gyro": gyro_on,
    }


class StatusReporter:
    """Writes the status file right away when something changes, otherwise every few seconds."""

    HEARTBEAT_S = 5.0

    def __init__(self):
        self._last: dict | None = None
        self._written_at = 0.0

    def update(self, now: float, fields: dict) -> None:
        if fields == self._last and now - self._written_at < self.HEARTBEAT_S:
            return
        try:
            status_file.write(fields)
        except OSError:
            pass  # the status file is a convenience; never let it stop the controller
        self._last, self._written_at = fields, now


def led_backup_folder() -> str:
    return os.path.join(os.path.dirname(config_path()), "controller-backups")


class LedApplier:
    """Sends the LED settings to the controller when they change. They are never saved on the controller.

    "controller" means the controller's own lights: the first time the app changes them, it keeps a copy
    of what the controller showed (led-profile<N>-original.bin), and puts that back when asked.
    """

    def __init__(self, pad: Controller, backup_folder: str | None = None):
        self.pad = pad
        self.backup_folder = backup_folder or led_backup_folder()
        self._applied: LedSettings | None = None  # None = the controller's own lights are showing
        self._profile: int | None = None
        self._original: bytes | None = None

    def apply(self, wanted: LedSettings) -> None:
        target = None if wanted.effect == "controller" else wanted
        if target == self._applied:
            return
        try:
            original = self._original_lights()
            blob = original if target is None else build_blob(target, *geometry(original))
            if not self.pad.write_led(self._profile, blob):
                raise DeviceError("the controller didn't accept the LED settings")
        except (DeviceError, ValueError, OSError) as err:
            log(f"lights: couldn't change them: {err}")
        else:
            log("lights: " + ("controller's own lights" if target is None else describe_lights(target)))
        self._applied = target  # after a failure, try again only when the settings change

    def _original_lights(self) -> bytes:
        if self._original is not None:
            return self._original
        self._profile = self.pad.read_active_profile()
        if self._profile is None:
            raise DeviceError("the controller didn't say which profile it's using")
        path = os.path.join(self.backup_folder, f"led-profile{self._profile}-original.bin")
        try:
            with open(path, "rb") as f:
                original = f.read()
            geometry(original)
        except (OSError, ValueError):
            original = self.pad.read_led(self._profile)
            if not original:
                raise DeviceError("couldn't read the controller's LED settings") from None
            geometry(original)
            os.makedirs(self.backup_folder, exist_ok=True)
            with open(path, "wb") as f:
                f.write(original)
        self._original = original
        return original


def hide_xpad(pad: Controller) -> tuple[str | None, evdev.InputDevice | None]:
    """Hide the basic Xbox pad of the same controller so games only see the virtual one.

    Returns (usb_dir, None) when the xpad driver was removed (Steam and games stop listing it),
    (None, device) when it could only be silenced by grabbing it (still listed), or (None, None).
    """
    try:
        usb_dir = detach_xpad(pad.path)
        if usb_dir:
            return usb_dir, None
    except PermissionError:
        log("note: no access to the controller's USB device, so the basic Xbox pad is only silenced "
            "and Steam will still list it. See 'Permissions' in README.md.")
    except OSError as err:
        log(f"note: couldn't remove the basic Xbox pad ({err}); silencing it instead.")

    path = find_xpad_event(pad.path)
    if path is None:
        log("note: no basic Xbox pad found for this controller, so there's nothing to hide.")
        return None, None
    try:
        device = evdev.InputDevice(path)
        device.grab()
    except OSError as err:
        log(f"warning: couldn't hide the basic Xbox pad ({path}): {err}. Games may see two controllers.")
        return None, None
    return None, device


def create_virtual_devices() -> tuple[VirtualElite, VirtualKeyboardMouse]:
    try:
        vpad = VirtualElite()
    except OSError as err:
        raise DeviceError(f"Couldn't create the virtual controller ({err}). Check access to /dev/uinput.") from err
    try:
        keyboard_mouse = VirtualKeyboardMouse()
    except OSError as err:
        vpad.close()
        raise DeviceError(f"Couldn't create the virtual keyboard and mouse ({err}). Check access to /dev/uinput.") from err
    log(f"controller active: virtual Xbox Elite controller ({vpad.device_path}) "
        f"and keyboard and mouse ({keyboard_mouse.device_path}) created")
    return vpad, keyboard_mouse


def run_connection(pad: Controller, watcher: ConfigWatcher, reporter: StatusReporter, hide: bool, verbose: bool) -> None:
    """Serve one controller connection (cable or dongle) until it goes away."""
    watcher.check(time.monotonic())
    games = GameWatcher()
    games.check(time.monotonic(), watcher.settings.profiles)
    settings = watcher.settings.for_profile(games.active)
    gyro = GyroAim(settings.gyro)

    detached, grabbed = hide_xpad(pad) if hide else (None, None)
    details = ""
    if pad.info:
        note = firmware_note(pad.info.firmware)
        details = f" · firmware {pad.info.firmware}{f' ({note})' if note else ''} · {pad.info.connection}"
    if detached:
        details += " · basic Xbox pad removed"
    elif grabbed:
        details += f" · basic Xbox pad silenced ({grabbed.path})"
    log(f"connected: {pad.path}{details} · press {gyro.settings.button} to toggle gyro aiming")
    if games.active:
        log(f"profile: {games.active}")
    lights = LedApplier(pad)
    lights.apply(settings.led)
    flash = PressFlash()
    flash.configure(settings.led)
    held = frozenset()  # buttons down, for "Flash on button press"

    vpad: VirtualElite | None = None
    keyboard_mouse: VirtualKeyboardMouse | None = None
    last_report = time.monotonic()
    next_info = last_report + INFO_REFRESH_S  # the handshake already read the battery once
    sent_rumble, last_rumble_send = (0, 0), 0.0
    cue, cue_until = (0, 0), 0.0
    last_buttons = frozenset()
    try:
        while True:
            select.select([pad, vpad] if vpad else [pad], [], [], 0.05)
            now = time.monotonic()
            reloaded = watcher.check(now)
            profile_changed = games.check(now, watcher.settings.profiles)
            if profile_changed:
                log(f"profile: {games.active}" if games.active else "profile: none (main settings)")
            if reloaded or profile_changed:
                settings = watcher.settings.for_profile(games.active)
                gyro.settings = settings.gyro
                lights.apply(settings.led)
                flash.configure(settings.led)
            if now >= next_info:
                pad.request_info()  # the answer updates pad.info (battery) on a later poll
                next_info = now + INFO_REFRESH_S

            states = pad.poll(0)
            if states:
                last_report = now
                if vpad is None:
                    vpad, keyboard_mouse = create_virtual_devices()
                for state in states:
                    vpad.update(settings.for_games(state))
                    keyboard_mouse.hold(settings.keys_for(state.buttons))
                held = frozenset().union(*(state.buttons for state in states))  # a quick tap between two loops still counts
                dx, dy, toggled = gyro.process(states, now)
                keyboard_mouse.move(dx, dy)
                if toggled:
                    log(f"gyro aiming {'on' if gyro.enabled else 'off'}")
                    cue, length = GYRO_CUE_ON if gyro.enabled else GYRO_CUE_OFF
                    cue_until = now + length
                if verbose and states[-1].buttons != last_buttons:
                    last_buttons = states[-1].buttons
                    log("buttons: " + (" ".join(n for n in protocol.BUTTON_NAMES if n in last_buttons) or "-"))
            elif vpad and now - last_report > IDLE_S:
                vpad.close()
                keyboard_mouse.close()  # also releases any keys still held
                vpad = keyboard_mouse = None
                sent_rumble = (0, 0)
                log("controller idle (off or asleep): virtual controller and keyboard and mouse removed")

            flash_color = flash.update(held if vpad is not None else frozenset(), now)
            if flash_color is not None:
                pad.set_led_color(*flash_color)

            if vpad:
                vpad.handle_events(now)
                wanted = cue if now < cue_until else vpad.rumble  # the gyro cue briefly overrides games
                refresh_due = any(wanted) and now - last_rumble_send >= RUMBLE_REFRESH_S
                if wanted != sent_rumble or refresh_due:
                    pad.rumble(*wanted)
                    if verbose and wanted != sent_rumble:
                        log(f"rumble: strong {wanted[0]} weak {wanted[1]}")
                    sent_rumble, last_rumble_send = wanted, now

            reporter.update(now, status_fields(pad, vpad is not None, gyro.enabled, games.active))
    finally:
        for device in (vpad, keyboard_mouse):
            if device:
                device.close()
        if detached:
            try:
                reattach_xpad(detached)
            except OSError:
                pass  # controller unplugged; the kernel sets it up again when it's back
        if grabbed:
            try:
                grabbed.ungrab()
            except OSError:
                pass  # device already gone
            grabbed.close()


def _exit_on_signal(signum, _frame) -> None:
    raise SystemExit(128 + signum)  # unwinds normally, so everything gets switched off


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Flydigi Vader 5 Pro as a virtual Xbox Elite controller.")
    parser.add_argument("--device", help="hidraw node to use (default: auto-detect)")
    parser.add_argument("--no-hide", action="store_true", help="don't hide the basic Xbox pad (games will see two controllers)")
    parser.add_argument("--verbose", "-v", action="store_true", help="print button presses and rumble")
    args = parser.parse_args(argv)

    lock = single_instance_lock("vader5-pad")
    if lock is None:
        print("vader5-pad: already running (perhaps as the service; stop it with ./vader5-service off).", file=sys.stderr)
        return 1

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _exit_on_signal)

    log("started")
    watcher = ConfigWatcher(log=log)
    reporter = StatusReporter()
    reporter.update(time.monotonic(), status_fields(None, False, False))
    last_error = None
    try:
        while True:
            try:
                with Controller(args.device) as pad:
                    last_error = None
                    run_connection(pad, watcher, reporter, not args.no_hide, args.verbose)
            except DeviceError as err:
                if str(err) != last_error:  # don't repeat the same message every second
                    log(f"{err}\n  Waiting for the controller...")
                    last_error = str(err)
                reporter.update(time.monotonic(), status_fields(None, False, False))
                time.sleep(RECONNECT_S)
    except KeyboardInterrupt:
        pass
    finally:
        status_file.remove()
        log("stopped")
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
