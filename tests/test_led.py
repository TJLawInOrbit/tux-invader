import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from vader5 import led, pad, protocol  # noqa: E402

ZONES, FRAMES = 10, 10
RED, BLUE = (255, 0, 0), (0, 0, 255)
# the header of the LED data read from the real controller (factory rainbow, effect 7, 10 zones)
FACTORY_HEADER = bytes.fromhex("000300000904140a0700ffffffffffffffffffff")
FACTORY = FACTORY_HEADER + bytes(ZONES * FRAMES * 3)


def frames_of(blob):
    body = blob[led.HEADER_SIZE:]
    return [[tuple(body[f * ZONES * 3 + z * 3:f * ZONES * 3 + z * 3 + 3]) for z in range(ZONES)] for f in range(FRAMES)]


def build(effect, colors=(RED,), brightness=50, speed=5):
    return led.build_blob(led.LedSettings(effect, tuple(colors), brightness, speed), ZONES, FRAMES)


class BlobTests(unittest.TestCase):
    def test_geometry_of_the_real_controller(self):
        self.assertEqual(led.geometry(FACTORY), (10, 10))
        with self.assertRaises(ValueError):
            led.geometry(b"\x00" * 10)

    def test_static_matches_what_worked_on_the_controller(self):
        blob = build("static", [BLUE], brightness=50)
        self.assertEqual(len(blob), 320)
        self.assertEqual(blob[:10], bytes([0, 3, 0, 0, 0, led.SPEED_LOOP_TIME[4], 50, 10, 5, 0]))
        self.assertEqual(blob[10:20], b"\xff" * 10)
        frames = frames_of(blob)
        self.assertEqual(frames[0], [BLUE] * ZONES)
        self.assertTrue(all(color == (0, 0, 0) for frame in frames[1:] for color in frame))

    def test_off(self):
        blob = build("off")
        self.assertEqual(blob[8], 6)
        self.assertEqual(set(blob[led.HEADER_SIZE:]), {0})

    def test_zones(self):
        frames = frames_of(build("zones", [RED, BLUE]))
        self.assertEqual(frames[0][:4], [RED, BLUE, RED, BLUE])

    def test_breath_uses_even_frames(self):
        blob = build("breath", [RED, BLUE])
        self.assertEqual((blob[8], blob[4]), (2, 3))  # effect, last frame
        frames = frames_of(blob)
        self.assertEqual((frames[0][0], frames[1][0], frames[2][0]), (RED, (0, 0, 0), BLUE))

    def test_color_cycle(self):
        blob = build("color_cycle", [RED, BLUE, (0, 255, 0)])
        self.assertEqual((blob[8], blob[4]), (3, 2))

    def test_press_flash_keeps_the_strip_dark_between_presses(self):
        blob = build("press_flash")
        self.assertEqual((blob[8], blob[2]), (6, 0))  # off; the app lights it on presses
        self.assertEqual(blob[5], led.FLASH_LOOP_TIME)  # quickest color changes, whatever the speed
        self.assertEqual(set(blob[led.HEADER_SIZE:]), {0})

    def test_animated_effects_use_frames(self):
        rainbow = build("rainbow", colors=())
        self.assertEqual((rainbow[8], rainbow[4]), (7, 9))
        self.assertEqual(len({color for frame in frames_of(rainbow) for color in frame}), 30)

        strobe = frames_of(build("strobe"))
        self.assertEqual((strobe[0][0], strobe[1][0]), (RED, (0, 0, 0)))
        self.assertEqual(build("strobe")[4], 1)

        pulse = [frame[0][0] for frame in frames_of(build("pulse"))]
        self.assertEqual(max(pulse), 255)
        self.assertEqual(pulse[-1], 0)

        wave = frames_of(build("wave"))
        self.assertEqual([frame.index(RED) for frame in wave], list(range(10)))

    def test_speed_and_brightness(self):
        self.assertEqual(build("rainbow", (), speed=10)[5], 2)
        self.assertEqual(build("rainbow", (), speed=1)[5], 30)
        self.assertEqual(build("rainbow", (), speed=9)[5], 4)  # the factory rainbow's value
        self.assertEqual(build("static", brightness=80)[6], 80)

    def test_controller_effect_sends_nothing(self):
        with self.assertRaises(ValueError):
            build("controller")

    def test_colors(self):
        self.assertEqual(led.parse_color("#FF8800"), (255, 136, 0))
        self.assertEqual(led.parse_color("0080ff"), (0, 128, 255))
        self.assertIsNone(led.parse_color("#12"))
        self.assertIsNone(led.parse_color("#gggggg"))
        self.assertEqual(led.color_text((255, 136, 0)), "#ff8800")


class PressFlashTests(unittest.TestCase):
    def test_lights_while_held_then_goes_dark(self):
        flash = led.PressFlash()
        self.assertIsNone(flash.update(True, 0.0))  # not configured for press_flash
        flash.configure(led.LedSettings("press_flash", ((200, 100, 0),), brightness=50))
        self.assertIsNone(flash.update(False, 0.0))  # nothing pressed, nothing to send
        self.assertEqual(flash.update(True, 0.1), (100, 50, 0))  # brightness 50 %
        self.assertIsNone(flash.update(True, 0.2))  # still held: already lit
        fade = [flash.update(False, 0.3 + step * 0.02) for step in range(2)]
        self.assertEqual(fade, [(0, 0, 0), None])  # dark as soon as it's let go

    def test_each_press_uses_the_next_color(self):
        flash = led.PressFlash()
        colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255))  # only 4 are used
        flash.configure(led.LedSettings("press_flash", colors, brightness=100))
        seen, now = [], 0.0
        for _ in range(5):
            seen.append(flash.update(True, now))
            self.assertIsNone(flash.update(True, now + 0.05))  # still held: same color, nothing to send
            flash.update(False, now + 0.1)  # let go
            now += 0.2
        self.assertEqual(seen, [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 0)])

    def test_fade_steps_are_paced_but_presses_are_sent_at_once(self):
        flash = led.PressFlash()
        flash.configure(led.LedSettings("press_flash", ((255, 0, 0), (0, 0, 255)), brightness=100))
        self.assertEqual(flash.update(True, 0.0), (255, 0, 0))
        self.assertIsNone(flash.update(False, 0.005))  # too soon after the press to send again
        self.assertEqual(flash.update(False, 0.02), (0, 0, 0))
        self.assertEqual(flash.update(True, 0.025), (0, 0, 255))  # pressed again: next color at once

    def test_other_effects_send_nothing(self):
        flash = led.PressFlash()
        flash.configure(led.LedSettings("static", ((255, 0, 0),)))
        self.assertIsNone(flash.update(True, 0.0))


class CommandTests(unittest.TestCase):
    def test_led_command_frames_match_the_ones_used_on_the_controller(self):
        self.assertEqual(protocol.led_read_request(0)[:7].hex(), "5aa5a7040014bf")
        self.assertEqual(protocol.led_write_start(0, 16)[:10].hex(), "5aa5a80600001014d2" + "00")
        pack = protocol.led_write_pack(3, bytes(range(20)))
        self.assertEqual(pack[:5].hex(), "5aa5a91703")
        self.assertEqual(pack[25], (0xA9 + 0x17 + 3 + sum(range(20))) & 0xFF)
        self.assertEqual(protocol.led_test_color(255, 0, 0)[:9].hex(), "5aa5f505ff0000f900")  # the red that worked
        self.assertEqual(protocol.active_profile(bytes([0x5A, 0xA5, 0xA1, 1, 0, 5])), 1)  # Switch-mode twin of 1


class FakeController:
    def __init__(self, lights=FACTORY, profile=0):
        self.lights, self.profile = lights, profile
        self.reads, self.writes = 0, []

    def read_active_profile(self):
        return self.profile

    def read_led(self, profile):
        self.reads += 1
        return self.lights

    def write_led(self, profile, blob):
        self.writes.append((profile, blob))
        self.lights = blob
        return True


class LedApplierTests(unittest.TestCase):
    def test_applies_only_changes_and_restores_the_original(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = FakeController()
            lights = pad.LedApplier(controller, folder)
            lights.apply(led.LedSettings())  # "controller": nothing to do
            self.assertEqual((controller.reads, controller.writes), (0, []))

            static = led.LedSettings("static", (BLUE,), 50, 5)
            lights.apply(static)
            self.assertEqual(controller.reads, 1)
            self.assertEqual(controller.writes[-1], (0, build("static", [BLUE])))
            self.assertTrue(os.path.exists(os.path.join(folder, "led-profile0-original.bin")))

            lights.apply(static)  # unchanged: nothing sent
            self.assertEqual(len(controller.writes), 1)

            lights.apply(led.LedSettings())  # back to the controller's own lights
            self.assertEqual(controller.writes[-1], (0, FACTORY))

    def test_original_comes_from_the_saved_copy_after_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            first = FakeController()
            pad.LedApplier(first, folder).apply(led.LedSettings("static", (RED,), 50, 5))
            restarted = FakeController(lights=first.lights)  # still showing the app's red
            lights = pad.LedApplier(restarted, folder)
            lights.apply(led.LedSettings("off"))
            lights.apply(led.LedSettings())
            self.assertEqual(restarted.reads, 0)  # used the saved original, not the red on the strip
            self.assertEqual(restarted.writes[-1], (0, FACTORY))


if __name__ == "__main__":
    unittest.main()
