"""Live view of everything the Vader 5 Pro reports: buttons, sticks, triggers, gyro, accelerometer.

    ./vader5-viewer            full-screen terminal view
    ./vader5-viewer --text     plain printed lines (add --raw for report bytes)
"""

from __future__ import annotations

import argparse
import curses
import locale
import os
import signal
import sys
import time

from . import protocol
from .device import Controller, DeviceError, find_config_hidraws, switch_off

FPS = 30
RUMBLE_PULSE_S = 0.4
RUMBLE_LEVEL = 180
INFO_REFRESH_S = 5.0
MIN_W, MIN_H = 80, 24

STICK_W, STICK_H = 19, 7  # interior of the stick box
GYRO_BAR_LIMIT_DPS = 500.0
ACCEL_BAR_LIMIT_G = 2.0

LATCH_S = 0.3  # keep a press lit this long so short pulses (Turbo) are visible

FACE_BUTTONS = ("A", "B", "X", "Y", "LB", "RB", "L3", "R3", "SELECT", "START", "HOME")
BACK_BUTTONS = ("M1", "M2", "M3", "M4")
EXTRA_BUTTONS = ("C", "Z", "LM", "RM")
OTHER_BUTTONS = ("FN", "TURBO")


def describe_info(info: protocol.ControllerInfo | None) -> str:
    if info is None:
        return "waiting for controller info..."
    return f"firmware {info.firmware} · {info.connection} · battery {info.battery}"


def format_unknown(state: protocol.InputState) -> str:
    return ", ".join(f"byte {offset} = 0x{bits:02X}" for offset, bits in state.unknown_bits)


# ---------------------------------------------------------------- text mode


def run_text(pad: Controller, seconds: float, show_raw: bool) -> None:
    deadline = time.monotonic() + seconds if seconds else None
    pad.request_info()
    printed_info = False
    latest = None
    count = 0
    window = time.monotonic()
    next_print = window + 0.25
    while deadline is None or time.monotonic() < deadline:
        states = pad.poll(0.02)
        count += len(states)
        if states:
            latest = states[-1]
        if pad.info and not printed_info:
            print(f"Flydigi {pad.info.model} · {describe_info(pad.info)} · {pad.path}", flush=True)
            printed_info = True
        now = time.monotonic()
        if latest and now >= next_print:
            print(format_line(latest, count / (now - window)), flush=True)
            if show_raw:
                print("   raw", latest.raw.hex(" "), flush=True)
            count, window, next_print = 0, now, now + 0.25


def format_line(s: protocol.InputState, rate: float) -> str:
    gx, gy, gz = s.gyro_dps
    ax, ay, az = s.accel_g
    buttons = " ".join(name for name in protocol.BUTTON_NAMES if name in s.buttons) or "-"
    line = (
        f"{rate:4.0f}/s  L({s.left_stick[0]:+6d},{s.left_stick[1]:+6d}) "
        f"R({s.right_stick[0]:+6d},{s.right_stick[1]:+6d}) LT{s.left_trigger:3d} RT{s.right_trigger:3d}  "
        f"gyro°/s({gx:+7.1f},{gy:+7.1f},{gz:+7.1f}) accel g({ax:+.2f},{ay:+.2f},{az:+.2f})  [{buttons}]"
    )
    if s.unknown_bits:
        line += f"  unmapped: {format_unknown(s)}"
    return line


# ---------------------------------------------------------------- full-screen mode


class Screen:
    def __init__(self, stdscr):
        self.win = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        self.dim = curses.A_DIM
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_GREEN)
            curses.init_pair(2, curses.COLOR_CYAN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            self.on = curses.color_pair(1) | curses.A_BOLD
            self.head = curses.color_pair(2) | curses.A_BOLD
            self.warn = curses.color_pair(3) | curses.A_BOLD
        else:
            self.on = curses.A_REVERSE | curses.A_BOLD
            self.head = curses.A_BOLD
            self.warn = curses.A_BOLD

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.win.getmaxyx()
        if y >= h or x >= w - 1:
            return
        try:
            self.win.addstr(y, x, text[: w - x - 1], attr)
        except curses.error:
            pass

    def chips(self, y: int, x: int, names, pressed) -> None:
        for name in names:
            self.put(y, x, f" {name} ", self.on if name in pressed else self.dim)
            x += len(name) + 3


def fill_bar(value: float, maximum: float, width: int) -> str:
    n = round(max(0.0, min(value, maximum)) / maximum * width)
    return "█" * n + "·" * (width - n)


def center_bar(value: float, limit: float, width: int) -> str:
    half = width // 2
    n = round(min(abs(value), limit) / limit * half)
    left = "·" * (half - n) + "█" * n if value < 0 else "·" * half
    right = "█" * n + "·" * (half - n) if value > 0 else "·" * half
    return f"{left}│{right}"


def draw_stick(scr: Screen, y: int, x: int, title: str, stick: tuple[int, int], clicked: bool) -> None:
    sx, sy = stick
    scr.put(y, x, title, scr.head)
    if clicked:
        scr.put(y, x + len(title) + 1, " click ", scr.on)
    scr.put(y + 1, x, "┌" + "─" * STICK_W + "┐")
    for row in range(STICK_H):
        scr.put(y + 2 + row, x, "│" + " " * STICK_W + "│")
    scr.put(y + 2 + STICK_H, x, "└" + "─" * STICK_W + "┘")
    scr.put(y + 2 + STICK_H // 2, x + 1 + STICK_W // 2, "┼", scr.dim)
    col = round((sx + 32768) / 65535 * (STICK_W - 1))
    row = round((32767 - sy) / 65535 * (STICK_H - 1))  # positive y drawn upward
    scr.put(y + 2 + row, x + 1 + col, "●", scr.head)
    scr.put(y + 3 + STICK_H, x, f"x {sx:+6d}   y {sy:+6d}")


def draw(scr: Screen, pad: Controller, state, rate: float, show_raw: bool, rumbling: bool,
         shown_buttons: frozenset[str]) -> None:
    win = scr.win
    win.erase()
    h, w = win.getmaxyx()
    if h < MIN_H or w < MIN_W:
        scr.put(0, 0, f"Window too small ({w}x{h}); need {MIN_W}x{MIN_H}. Press q to quit.", scr.warn)
        win.refresh()
        return

    model = pad.info.model if pad.info else "Vader 5 Pro"
    scr.put(0, 2, f"Flydigi {model}", scr.head)
    scr.put(0, 4 + len(model) + 8, describe_info(pad.info))
    scr.put(1, 2, f"{pad.path} · {rate:.0f} reports/s · test mode on while this runs", scr.dim)

    footer = "[r] rumble  [1] strong motor  [2] weak motor  [h] raw bytes  [q] quit"
    scr.put(h - 1, 2, footer, scr.dim)
    if rumbling:
        scr.put(h - 1, 4 + len(footer), "~ rumbling ~", scr.warn)

    if state is None:
        scr.put(3, 2, "Waiting for input reports...", scr.warn)
        win.refresh()
        return

    pressed = shown_buttons  # includes presses from the last moment, so short pulses stay visible
    draw_stick(scr, 3, 2, "Left stick", state.left_stick, "L3" in pressed)
    draw_stick(scr, 3, 26, "Right stick", state.right_stick, "R3" in pressed)

    x = 51
    scr.put(3, x, "Triggers", scr.head)
    for row, name, value in ((4, "LT", state.left_trigger), (5, "RT", state.right_trigger)):
        scr.put(row, x, name, scr.on if name in pressed else 0)  # lit = digital press bit
        scr.put(row, x + 3, f"{fill_bar(value, 255, 18)} {value:3d}")
    scr.put(7, x, "D-pad", scr.head)
    scr.chips(8, x + 6, ["UP"], pressed)
    scr.chips(9, x, ["LEFT"], pressed)
    scr.chips(9, x + 11, ["RIGHT"], pressed)
    scr.chips(10, x + 5, ["DOWN"], pressed)
    scr.put(12, x, "Back", scr.head)
    scr.chips(12, x + 6, BACK_BUTTONS, pressed)
    scr.put(13, x, "Extra", scr.head)
    scr.chips(13, x + 6, EXTRA_BUTTONS, pressed)
    scr.put(14, x, "Other", scr.head)
    scr.chips(14, x + 6, OTHER_BUTTONS, pressed)

    scr.put(15, 2, "Buttons", scr.head)
    scr.chips(15, 10, FACE_BUTTONS, pressed)
    if state.unknown_bits:
        scr.put(16, 2, f"Unmapped bits: {format_unknown(state)}  <- a button we haven't named yet", scr.warn)
    else:
        scr.put(16, 2, "Unmapped bits: none", scr.dim)

    scr.put(18, 2, "Gyro (°/s)", scr.head)
    scr.put(18, 42, "Accelerometer (g)", scr.head)
    for i, axis in enumerate("XYZ"):
        dps = state.gyro_dps[i]
        g = state.accel_g[i]
        scr.put(19 + i, 2, f"{axis} {center_bar(dps, GYRO_BAR_LIMIT_DPS, 21)} {dps:+7.1f}")
        scr.put(19 + i, 42, f"{axis} {center_bar(g, ACCEL_BAR_LIMIT_G, 21)} {g:+5.2f}")

    if show_raw:
        scr.put(22, 2, "bytes 11-31: " + state.raw[11:32].hex(" "), scr.dim)
    win.refresh()


def run_ui(stdscr, pad: Controller) -> None:
    scr = Screen(stdscr)
    state = None
    last_pressed: dict[str, float] = {}  # button -> last time it was seen pressed
    show_raw = False
    rumbling = False
    rumble_until = 0.0
    rate, count, window = 0.0, 0, time.monotonic()
    next_frame = next_info = 0.0

    while True:
        states = pad.poll(min(max(0.0, next_frame - time.monotonic()), 1 / FPS))
        now = time.monotonic()
        if states:
            state = states[-1]
            count += len(states)
            for s in states:
                for name in s.buttons:
                    last_pressed[name] = now
        if now - window >= 1.0:
            rate, count, window = count / (now - window), 0, now
        if now >= next_info:
            pad.request_info()
            next_info = now + INFO_REFRESH_S
        if rumbling and now >= rumble_until:
            pad.rumble(0, 0)
            rumbling = False

        key = stdscr.getch()
        while key != -1:
            if key in (ord("q"), ord("Q"), 27):
                return
            if key in (ord("r"), ord("R"), ord("1"), ord("2")):
                strong = RUMBLE_LEVEL if key != ord("2") else 0
                weak = RUMBLE_LEVEL if key != ord("1") else 0
                pad.rumble(strong, weak)
                rumbling, rumble_until = True, now + RUMBLE_PULSE_S
            elif key in (ord("h"), ord("H")):
                show_raw = not show_raw
            key = stdscr.getch()

        if now >= next_frame:
            shown = frozenset(name for name, seen in last_pressed.items() if now - seen < LATCH_S)
            draw(scr, pad, state, rate, show_raw, rumbling, shown)
            next_frame = now + 1 / FPS


# ---------------------------------------------------------------- entry point


def _exit_on_signal(signum, _frame) -> None:
    raise SystemExit(128 + signum)  # unwinds normally, so test mode gets switched off


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live view of the Flydigi Vader 5 Pro's inputs.")
    parser.add_argument("--device", help="hidraw node to use (default: auto-detect)")
    parser.add_argument("--text", action="store_true", help="print readings as lines instead of the full-screen view")
    parser.add_argument("--seconds", type=float, default=0, help="with --text: stop after this many seconds")
    parser.add_argument("--raw", action="store_true", help="with --text: also print the raw report bytes")
    parser.add_argument("--reset", action="store_true", help="switch test mode and rumble off (if a crash left them on) and exit")
    parser.add_argument("--list", action="store_true", help="list connected controller connections (cable/dongle) and exit")
    args = parser.parse_args(argv)

    if args.list:
        found = find_config_hidraws()
        for path, description in found:
            print(f"{path}  {description}")
        if not found:
            print("No Flydigi controller found.")
        return 0

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _exit_on_signal)
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")

    try:
        if args.reset:
            switch_off(args.device)
            print("Test mode and rumble switched off; basic Xbox pad restored if it had been removed.")
            return 0
        with Controller(args.device) as pad:
            if args.text:
                run_text(pad, args.seconds, args.raw)
            else:
                curses.wrapper(run_ui, pad)
    except DeviceError as e:
        print(f"vader5-viewer: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Output was cut off (e.g. piped into `head`); the controller is already switched off.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
