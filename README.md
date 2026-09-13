# Vader 5 Pro on Linux

A small userspace app for the Flydigi Vader 5 Pro. It gets the back paddles, extra buttons, gyro,
accelerometer and rumble working on Linux, without Flydigi software, kernel modules or driver changes.

## How it works

The controller has a vendor HID interface (`/dev/hidrawN`, usage page `0xFFA0`). After a short
handshake, a "test mode" command makes it stream a complete input report at about 500 Hz. The app
turns test mode on while it runs and switches it off again when it exits.

## Run the live viewer

```sh
./vader5-viewer            # full-screen view (needs an 80x24 terminal)
./vader5-viewer --text     # printed lines; add --raw for bytes, --seconds 5 to stop
```

Keys: `r` rumble, `1` strong motor, `2` weak motor, `h` raw bytes, `q` quit.

Quitting normally, Ctrl+C and closing the terminal all switch test mode off. If the app is
force-killed (`kill -9`, a crash), the controller keeps streaming in test mode until it's unplugged
or you run `./vader5-viewer --reset`.

If a button lights up under **Unmapped bits**, it isn't named yet. Note which physical button it
was and add it to `BUTTON_BITS` in `vader5/protocol.py`.

## Use it in games (virtual Xbox Elite controller)

```sh
./vader5-pad            # keep it running while you play; Ctrl+C to stop
./vader5-pad --verbose  # also print button presses and rumble
```

Games and Steam see an Xbox Elite Series 2 controller. The controller's basic Xbox pad is hidden
while this runs, so nothing is pressed twice. Rumble from games is passed to the controller. If you
unplug, switch between cable and dongle, or turn the controller off, it reconnects on its own.

| Vader 5 Pro | Virtual controller |
|-------------|--------------------|
| Sticks, triggers, A B X Y, LB RB, L3 R3, D-pad | Same Xbox buttons and axes |
| Select, Start, Home | Back (View), Start (Menu), Guide |
| M1 / M2 / M3 / M4 | Paddles: upper left / upper right / lower left / lower right |
| C, Z, LM, RM, FN | Extra buttons 1-5 (bindable in Steam and some games) |
| Turbo | Toggles gyro aiming (not sent to games) |

### Gyro aiming

Press **Turbo** to turn gyro aiming on or off. A short light buzz means on; a longer heavy buzz means
off. While it's on, turning the controller left and right moves the mouse sideways, and tilting it
up and down moves it vertically, on top of your normal controller input. Games see an ordinary
mouse ("Vader 5 Pro Keyboard and Mouse"), so it works in any game that lets you aim with the mouse
while you use a controller. Turbo itself isn't sent to games. Gyro aiming starts off each time the controller
connects.

Drift is calibrated out automatically whenever the controller is held still for a moment, for
example resting in your lap.

The toggle button, sensitivity and direction can be changed in the settings file (see **Settings**).

### Settings

```sh
./vader5-config create   # write a starter settings file with explanations
./vader5-config check    # show mistakes, or the settings in use
./vader5-config path     # where the file is: ~/.config/vader5/config.toml
```

Edit the file and save it: `vader5-pad` applies the change within about a second, with no restart.
If the file has a mistake, `./vader5-config check` and the service log (`./vader5-service logs`) say
what's wrong, and the previous settings stay in use.

- `[gyro]`: toggle button, sensitivity, inverting either direction, tightening
- `[sticks]`: deadzones, for a stick that drifts
- `[remap]`: make a button send something else:
  - another controller button: `M1 = "A"`
  - a keyboard key or combination: `M2 = "key:space"`, `M3 = "key:ctrl+c"`
  - a mouse button: `M4 = "mouse:right"` (`left`, `right`, `middle`, `back`, `forward`)
  - nothing: `RM = "NONE"`

  Key names are like `a`, `1`, `space`, `enter`, `esc`, `tab`, `ctrl`, `shift`, `alt`, `super`,
  `f1` and `f13`; the full list is in `/usr/include/linux/input-event-codes.h`, without `KEY_`.
  Keys are released when you let go of the button, and also if the controller turns off.

### Start automatically

```sh
./vader5-service install    # run now and every time you log in (a systemd user service, no root)
./vader5-service off        # stop until next login; the basic Xbox pad comes back
./vader5-service on         # start again
./vader5-service status     # is it running? latest messages
./vader5-service logs       # follow messages live
./vader5-service restart    # after updating the code
./vader5-service uninstall  # remove the service
```

The virtual controller appears when the controller starts sending input and disappears about
3 seconds after it stops (switched off, asleep or unplugged), so Steam never lists a controller
that's off. Only one `vader5-pad` can run at a time: while the service is on, `./vader5-pad` in a
terminal will tell you it's already running. `./vader5-viewer` still works alongside it, but input
in games pauses for about half a second when the viewer quits.

### Seeing only one controller

With the udev rule from **Permissions** installed, `vader5-pad` takes the kernel's basic Xbox driver
off the controller while it runs, so Steam and games list only the Xbox Elite controller. When
`vader5-pad` stops, the basic pad comes back. If `vader5-pad` was force-killed, run
`./vader5-viewer --reset` or replug the controller.

Without the rule, the basic pad is only silenced: it sends nothing, but Steam and SDL games still
list it. For a Steam game you can hide it from the game with this launch option:
`SDL_GAMECONTROLLER_IGNORE_DEVICES=0x37d7/0x2401 %command%`

## Requirements

- Python 3.10+ (standard library only)
- Read/write access to the controller's hidraw node. Steam's `60-steam-input.rules` already grants
  this for Flydigi devices to the logged-in user.

### Permissions

`install/70-vader5-pro.rules` gives the logged-in user access to this controller only: its hidraw
interface (inputs, rumble, test mode) and its USB device, which lets `vader5-pad` remove the basic
Xbox pad while it runs. Steam's own udev rules grant the same kind of access for its controllers.

```sh
sudo install -m 644 install/70-vader5-pro.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

Then unplug and replug the controller (or the dongle). To undo:

```sh
sudo rm /etc/udev/rules.d/70-vader5-pro.rules
sudo udevadm control --reload-rules
```

## Tests

```sh
python3 -m unittest discover tests
```

## Layout

| File | Purpose |
|------|---------|
| `vader5/protocol.py` | Command packets and report decoding (no I/O) |
| `vader5/device.py` | Finds the controller, handshake, test mode on/off, rumble |
| `vader5/viewer.py` | Live viewer (curses and text mode) |
| `vader5/virtual_pad.py` | Virtual Xbox Elite controller (uinput): button mapping, rumble requests |
| `vader5/gyro.py` | Gyro aiming: toggle, drift calibration |
| `vader5/keyboard_mouse.py` | Virtual keyboard and mouse (uinput): gyro movement, keys for remapped buttons |
| `vader5/config.py`, `vader5-config` | Settings file: loading, checking, reloading on save |
| `vader5/pad.py` | Runs the virtual controller: hides the basic pad, forwards rumble, reconnects |
| `vader5-service` | Installs and controls `vader5-pad` as a systemd user service |

## Protocol notes

Commands are 32 bytes: `5A A5 <cmd> <args...> <checksum>`, where the checksum is the sum of every
byte after `5A A5`.

| Command | Bytes |
|---------|-------|
| Handshake | `5A A5 01 02 03`, `5A A5 A1 02 A3`, `5A A5 02 02 04`, `5A A5 04 02 06` |
| Test mode on / off | `5A A5 11 07 FF 01 FF FF FF 15` / `... 00 FF FF FF 14` |
| Rumble | `5A A5 12 06 <strong> <weak> 00 00 <checksum>` |

Input report (`5A A5 EF`, 32 bytes):

| Offset | Content |
|--------|---------|
| 3-10 | Left X, left Y, right X, right Y (int16 LE) |
| 11 | D-pad up `01`, right `02`, down `04`, left `08`; A `10`, B `20`, Select `40`, X `80` |
| 12 | Y `01`, Start `02`, LB `04`, RB `08`, LT pressed `10`, RT pressed `20`, L3 `40`, R3 `80` |
| 13 | C `01`, Z `02`, M1 `04`, M2 `08`, M3 `10`, M4 `20`, LM `40`, RM `80` |
| 14 | FN `01`, Turbo `02` (a ~50 ms pulse per press, however long it's held), Home `08` |
| 15, 16 | LT, RT (0-255) |
| 17-22 | Gyro X, Y, Z (int16 LE, ±32768 = ±2000 °/s) |
| 23-28 | Accel X, Y, Z (int16 LE, 4096 = 1 g) |
| 29 | Packet counter over the dongle (0 over the cable) |
| 30 | Unused so far |
| 31 | Checksum: sum of bytes 2-30 |

Sources: SDL's `SDL_hidapi_flydigi.c`, [BANANASJIM/flydigi-vader5](https://github.com/BANANASJIM/flydigi-vader5),
and captures from a real controller (firmware 7.1.4.0).

## Roadmap

1. Live viewer (done; tested over the cable and the 2.4G dongle)
2. Virtual Xbox Elite controller via uinput, hiding the basic Xbox pad, with rumble passthrough
   (working in Steam: single controller, rumble; being tested in games and emulators)
3. Start automatically as a systemd user service (done; tested over cable and dongle, including
   power off and on)
4. Later:
   - Gyro aiming in PC games (working; to revisit: center slips on fast turns, try player-space gyro)
   - Settings file: gyro, deadzones, remaps (in progress); per-game profiles later
   - Put the project under git (done)
   - Maybe: settings window, real motion sensor for games
