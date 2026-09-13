"""Run the Vader 5 Pro as a virtual Xbox Elite controller for games and Steam.

    ./vader5-pad               run until Ctrl+C
    ./vader5-pad --verbose     also print button presses and rumble
    ./vader5-service install   or run it automatically (see README.md)

The virtual controller and the gyro mouse only exist while the real controller is on and sending
input, so Steam and games don't list a controller that's switched off.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import select
import signal
import sys
import time

import evdev

from . import protocol
from .device import Controller, DeviceError, detach_xpad, find_xpad_event, reattach_xpad
from .gyro import GyroAim, VirtualMouse
from .virtual_pad import VirtualElite

RECONNECT_S = 1.0
IDLE_S = 3.0  # no input reports for this long: the controller is off or asleep
RUMBLE_REFRESH_S = 0.5  # re-send active rumble so it never lapses on the controller
GYRO_CUE_ON = ((0, 150), 0.08)  # (strong, weak), seconds: short light buzz = gyro aiming on
GYRO_CUE_OFF = ((150, 0), 0.25)  # longer heavy buzz = gyro aiming off


def log(message: str) -> None:
    print(f"vader5-pad: {message}", flush=True)


def single_instance_lock():
    """Lock held for as long as this process runs; None if another vader5-pad already holds it."""
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "vader5-pad.lock")
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


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


def create_virtual_devices() -> tuple[VirtualElite, VirtualMouse]:
    try:
        vpad = VirtualElite()
    except OSError as err:
        raise DeviceError(f"Couldn't create the virtual controller ({err}). Check access to /dev/uinput.") from err
    try:
        mouse = VirtualMouse()
    except OSError as err:
        vpad.close()
        raise DeviceError(f"Couldn't create the gyro mouse ({err}). Check access to /dev/uinput.") from err
    log(f"controller active: virtual Xbox Elite controller ({vpad.device_path}) and gyro mouse ({mouse.device_path}) created")
    return vpad, mouse


def run_connection(pad: Controller, hide: bool, verbose: bool) -> None:
    """Serve one controller connection (cable or dongle) until it goes away."""
    gyro = GyroAim()
    hidden = frozenset({gyro.settings.button})  # the gyro toggle isn't passed to games

    detached, grabbed = hide_xpad(pad) if hide else (None, None)
    details = f" · firmware {pad.info.firmware} · {pad.info.connection}" if pad.info else ""
    if detached:
        details += " · basic Xbox pad removed"
    elif grabbed:
        details += f" · basic Xbox pad silenced ({grabbed.path})"
    log(f"connected: {pad.path}{details} · press {gyro.settings.button} to toggle gyro aiming")

    vpad: VirtualElite | None = None
    mouse: VirtualMouse | None = None
    last_report = time.monotonic()
    sent_rumble, last_rumble_send = (0, 0), 0.0
    cue, cue_until = (0, 0), 0.0
    last_buttons = frozenset()
    try:
        while True:
            select.select([pad, vpad] if vpad else [pad], [], [], 0.05)
            now = time.monotonic()
            states = pad.poll(0)
            if states:
                last_report = now
                if vpad is None:
                    vpad, mouse = create_virtual_devices()
                for state in states:
                    vpad.update(state, hidden)
                dx, dy, toggled = gyro.process(states, now)
                mouse.move(dx, dy)
                if toggled:
                    log(f"gyro aiming {'on' if gyro.enabled else 'off'}")
                    cue, length = GYRO_CUE_ON if gyro.enabled else GYRO_CUE_OFF
                    cue_until = now + length
                if verbose and states[-1].buttons != last_buttons:
                    last_buttons = states[-1].buttons
                    log("buttons: " + (" ".join(n for n in protocol.BUTTON_NAMES if n in last_buttons) or "-"))
            elif vpad and now - last_report > IDLE_S:
                vpad.close()
                mouse.close()
                vpad = mouse = None
                sent_rumble = (0, 0)
                log("controller idle (off or asleep): virtual controller and gyro mouse removed")

            if vpad:
                vpad.handle_events(now)
                wanted = cue if now < cue_until else vpad.rumble  # the gyro cue briefly overrides games
                refresh_due = any(wanted) and now - last_rumble_send >= RUMBLE_REFRESH_S
                if wanted != sent_rumble or refresh_due:
                    pad.rumble(*wanted)
                    if verbose and wanted != sent_rumble:
                        log(f"rumble: strong {wanted[0]} weak {wanted[1]}")
                    sent_rumble, last_rumble_send = wanted, now
    finally:
        for device in (vpad, mouse):
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

    lock = single_instance_lock()
    if lock is None:
        print("vader5-pad: already running (perhaps as the service; stop it with ./vader5-service off).", file=sys.stderr)
        return 1

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _exit_on_signal)

    log("started")
    last_error = None
    try:
        while True:
            try:
                with Controller(args.device) as pad:
                    last_error = None
                    run_connection(pad, not args.no_hide, args.verbose)
            except DeviceError as err:
                if str(err) != last_error:  # don't repeat the same message every second
                    log(f"{err}\n  Waiting for the controller...")
                    last_error = str(err)
                time.sleep(RECONNECT_S)
    except KeyboardInterrupt:
        pass
    finally:
        log("stopped")
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
