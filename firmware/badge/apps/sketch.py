"""Sketch - draw on the badge with two knobs, shake to erase

Turn the left knob to move the pen left/right and the right knob to move it
up/down (clockwise = right / up). The line is drawn into the framebuffer as
you turn, and the panel redraws shortly after the knobs stop.

Refreshing: the red frame goes up once with a full refresh when the app
starts. After that every redraw is a fast refresh with keep_red=True, which
only drives black and white pixels, so the red frame isn't touched and
doesn't fade.

Buttons:
  User A    erase the drawing (fast refresh, red frame stays)
  User B    two knobs: full refresh, to clean up any ghosting
            one knob:  switch which axis the knob drives
  Menu +/-  back to the app menu

LEDs: 0-3 follow the X knob and 4-7 the Y knob while turning (a dot spins the
way you turn, and goes red when the pen hits the edge). When idle: green =
up to date, yellow = waiting to redraw, cyan = panel busy.

Hardware
--------
Knobs: AS5600 magnetic encoder SAOs with the sensor's analog OUT pin wired to
the SAO's GPIO1. All AS5600s share I2C address 0x36, so they're read through
the ADC instead. The app probes each SAO GPIO1 pin (GPIO2/4/7) with the
internal pull-down and then pull-up: a floating pin follows the pull, a pin
driven by an AS5600 doesn't. The first two found are X and Y. When knobs are
found, the AS5600 CONF register is written over the SAO I2C bus (every
AS5600 takes the same write) to set the output to 10-90 % of VDD, which keeps
it inside the ESP32-S3 ADC's ~3.1 V range.

Shake to erase (optional): an MPU6050 on the SAO I2C bus at 0x68 or 0x69.
It takes SHAKE_PEAKS jolts over SHAKE_G within SHAKE_WINDOW_MS, so bumps
don't wipe the drawing.
"""

import asyncio as aio
import machine
import time

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.epd import display
from lib.led_bar import leds

FRAME_PNG = "/data/graphics/sketch_frame.png"

LINE_W = 3              # line thickness in pixels
# The white screen in sketch_frame.png is x 70..890, y 58..532 with 18 px
# rounded corners. Keep the whole line inside it, clear of the corners.
_INSET = 12 + LINE_W
X_MIN, Y_MIN, X_MAX, Y_MAX = 70 + _INSET, 58 + _INSET, 890 - _INSET, 532 - _INSET

# SAO GPIO1 pins (see README), probed in this order
CANDIDATE_PINS = ((2, "SAO1"), (4, "SAO2"), (7, "SAO3"))
X_KNOB_PIN = None       # None = auto-detect, or a GPIO number to force it
Y_KNOB_PIN = None
SWAP_KNOBS = False      # if X and Y come out the wrong way round
INVERT_X = False
INVERT_Y = False

PX_PER_REV = 240        # pen travel per full knob turn
DEADBAND = 12           # ADC noise to ignore, in counts of 4096
ADC_SAMPLES = 8         # readings averaged per poll
IDLE_REFRESH_MS = 700   # redraw this long after the knobs stop...
MAX_WAIT_MS = 4000      # ...or at least this often while drawing

LED_TURN_HOLD_MS = 400  # keep the turning animation this long after a turn
LED_DOT_STEPS = 16      # dot steps per knob revolution
X_COLOR = "cyan"
Y_COLOR = "magenta"
WALL_COLOR = "red"

SHAKE_G = 2.2           # a jolt is a reading this far from 1 g
SHAKE_PEAKS = 4         # this many jolts...
SHAKE_WINDOW_MS = 1200  # ...within this long erases
SHAKE_COOLDOWN_MS = 3000

AS5600_ADDR = 0x36
_VDD_UV = 3_300_000


def set_reduced_analog_range():
    """CONF.OUTS = 01: analog output 10-90 % of VDD. Volatile, so it's redone
    each launch. Returns True if the write was ACKed."""
    try:
        from lib.sao import i2c
        # CONF is 0x07 (high byte) / 0x08 (low byte); OUTS is bits 5:4 of 0x08.
        i2c.writeto_mem(AS5600_ADDR, 0x07, bytes([0x00, 0x10]))
        return True
    except Exception as ex:
        print(f"Sketch: couldn't configure AS5600 ({ex}), assuming 0-100 % output")
        return False


def _avg_uv(adc, n):
    t = 0
    for _ in range(n):
        t += adc.read_uv()
    return t // n


def probe_pin(pin):
    """True if something is driving the pin (pull-down vs pull-up test)."""
    P = machine.Pin
    adc = machine.ADC(P(pin), atten=machine.ADC.ATTN_11DB)
    P(pin, P.IN, P.PULL_DOWN)
    time.sleep_ms(3)
    lo = _avg_uv(adc, 8)
    P(pin, P.IN, P.PULL_UP)
    time.sleep_ms(3)
    hi = _avg_uv(adc, 8)
    P(pin, P.IN, None)
    driven = abs(hi - lo) < 300_000
    print(f"Sketch: GPIO{pin} pulled down {lo / 1e6:.2f} V, up {hi / 1e6:.2f} V"
          f" -> {'knob' if driven else 'nothing'}")
    return driven


class AnalogKnob:
    """An AS5600 analog output read with the ADC."""

    def __init__(self, pin, label, reduced_range):
        self.label = f"{label}/GPIO{pin}"
        machine.Pin(pin, machine.Pin.IN, None)
        self.adc = machine.ADC(machine.Pin(pin), atten=machine.ADC.ATTN_11DB)
        if reduced_range:
            self.lo, self.hi = _VDD_UV * 10 // 100, _VDD_UV * 90 // 100
        else:
            self.lo, self.hi = 0, _VDD_UV
        self.last = None

    def __str__(self):
        return self.label

    def angle(self):
        a = (_avg_uv(self.adc, ADC_SAMPLES) - self.lo) * 4096 // (self.hi - self.lo)
        return min(max(a, 0), 4095)

    def delta(self):
        """Signed counts turned since the last accepted reading."""
        a = self.angle()
        if self.last is None:
            self.last = a
            return 0
        d = ((a - self.last + 2048) & 4095) - 2048
        if -DEADBAND <= d <= DEADBAND:
            return 0  # don't move `last`, so slow turns still add up
        self.last = a
        return d


def find_knobs():
    labels = {p: l for p, l in CANDIDATE_PINS}
    if X_KNOB_PIN is not None or Y_KNOB_PIN is not None:
        pins = [p for p in (X_KNOB_PIN, Y_KNOB_PIN) if p is not None]
    else:
        pins = [p for p, _ in CANDIDATE_PINS if probe_pin(p)][:2]
    if not pins:
        return []
    reduced = set_reduced_analog_range()
    knobs = []
    for pin in pins:
        try:
            knobs.append(AnalogKnob(pin, labels.get(pin, "SAO?"), reduced))
        except Exception as ex:
            print(f"Sketch: knob on GPIO{pin} failed: {ex}")
    return knobs


class Shake:
    """MPU6050 shake detector on the SAO I2C bus."""

    def __init__(self):
        self.addr = None
        self._peaks = []
        self._in_peak = False
        self._cooldown_until = 0
        try:
            from lib.sao import i2c
            self.i2c = i2c
            found = i2c.scan()
            for a in (0x68, 0x69):
                # WHO_AM_I: MPU6050 and close relatives
                if a in found and i2c.readfrom_mem(a, 0x75, 1)[0] in (0x68, 0x70, 0x71, 0x72, 0x73, 0x98):
                    self.addr = a
                    break
            if self.addr is None:
                return
            i2c.writeto_mem(self.addr, 0x6B, b"\x00")   # wake up
            i2c.writeto_mem(self.addr, 0x1C, b"\x10")   # accel +-8 g
            i2c.writeto_mem(self.addr, 0x1A, b"\x03")   # DLPF ~44 Hz
            print(f"Sketch: MPU6050 at 0x{self.addr:02X}, shake to erase")
        except Exception as ex:
            print(f"Sketch: MPU6050 setup failed: {ex}")
            self.addr = None

    def poll(self):
        """True once when a hard shake is detected."""
        if self.addr is None:
            return False
        now = time.ticks_ms()
        if time.ticks_diff(self._cooldown_until, now) > 0:
            return False
        try:
            d = self.i2c.readfrom_mem(self.addr, 0x3B, 6)
        except OSError:
            return False
        g2 = 0.0
        for i in (0, 2, 4):
            v = (d[i] << 8) | d[i + 1]
            if v & 0x8000:
                v -= 0x10000
            g2 += (v / 4096) ** 2   # 4096 LSB/g at +-8 g
        jolt = abs(g2 ** 0.5 - 1.0)
        if jolt > SHAKE_G and not self._in_peak:
            self._in_peak = True
            self._peaks.append(now)
        elif jolt < SHAKE_G * 0.5:
            self._in_peak = False
        self._peaks = [t for t in self._peaks if time.ticks_diff(now, t) < SHAKE_WINDOW_MS]
        if len(self._peaks) >= SHAKE_PEAKS:
            self._peaks = []
            self._cooldown_until = time.ticks_add(now, SHAKE_COOLDOWN_MS)
            return True
        return False


def big_text(fb, text, x, y, color, scale=2):
    """framebuf's 8x8 font, scaled up."""
    import framebuf
    w = 8 * len(text)
    tmp = framebuf.FrameBuffer(bytearray(w), w, 8, framebuf.MONO_HLSB)
    tmp.text(text, 0, 0, 1)
    for ty in range(8):
        for tx in range(w):
            if tmp.pixel(tx, ty):
                fb.fill_rect(x + tx * scale, y + ty * scale, scale, scale, color)


class SketchApp(BaseApp):
    def __init__(self, name=None) -> None:
        self.name = "Sketch"
        self.sleep_ms = 15
        self.launched = False
        self.knobs = None
        self.x_knob = None
        self.y_knob = None
        self.single_axis = "x"
        self.shake = None
        self.px = (X_MIN + X_MAX) // 2
        self.py = (Y_MIN + Y_MAX) // 2
        self._fx = 0.0
        self._fy = 0.0
        self._dirty = False
        self._last_move = 0
        self._first_change = 0
        self._phase = [0.0, 0.0]          # LED dot position per knob
        self._turn_ms = [None, None]      # last time each knob turned
        self._wall_ms = [None, None]      # last time each axis hit the edge
        self._led_frame = None

    # ---------------- setup ----------------
    def _setup(self):
        if not self.knobs:
            self.knobs = find_knobs()
            ks = list(reversed(self.knobs)) if SWAP_KNOBS else self.knobs
            self.x_knob = ks[0] if len(ks) > 0 else None
            self.y_knob = ks[1] if len(ks) > 1 else None
            print(f"Sketch: X knob {self.x_knob}, Y knob {self.y_knob}")
        if self.shake is None:
            self.shake = Shake()

    def _status_text(self):
        if self.x_knob and self.y_knob:
            return None
        if self.x_knob:
            return f"1 knob ({self.x_knob}) - B switches X/Y"
        return "no knobs found - see apps/sketch.py"

    def _blank(self, full):
        """Empty screen. full=True also puts the red frame up properly."""
        leds.fill("cyan")
        display.clear()
        display.draw_png(FRAME_PNG)
        msg = self._status_text()
        if msg:
            # White text on the red frame, under the screen
            big_text(display.red, msg, 200, 552, 0)
            big_text(display.black, msg, 200, 552, 0)
        self._pen_dot()
        if full:
            display.update()
        else:
            display.update(fast=True, keep_red=True)
        self._dirty = False
        self._led_frame = None

    def _pen_dot(self):
        r = LINE_W // 2
        display.black.fill_rect(self.px - r, self.py - r, LINE_W, LINE_W, 1)

    # ---------------- drawing ----------------
    def _line(self, x0, y0, x1, y1):
        lo = -(LINE_W // 2)
        for ox in range(lo, lo + LINE_W):
            for oy in range(lo, lo + LINE_W):
                display.black.line(x0 + ox, y0 + oy, x1 + ox, y1 + oy, 1)

    def _read_deltas(self):
        cx = cy = 0
        if self.x_knob and self.y_knob:
            cx = self.x_knob.delta()
            cy = self.y_knob.delta()
        elif self.x_knob:
            d = self.x_knob.delta()
            if self.single_axis == "x":
                cx = d
            else:
                cy = d
        return (-cx if INVERT_X else cx), (-cy if INVERT_Y else cy)

    def _poll_knobs(self):
        cx, cy = self._read_deltas()
        if not cx and not cy:
            return
        now = time.ticks_ms()
        if cx:
            self._turn_ms[0] = now
            self._phase[0] += cx * LED_DOT_STEPS / 4096
        if cy:
            self._turn_ms[1] = now
            self._phase[1] += cy * LED_DOT_STEPS / 4096

        scale = PX_PER_REV / 4096
        self._fx += cx * scale
        self._fy -= cy * scale          # clockwise = up
        mx, my = int(self._fx), int(self._fy)
        if not mx and not my:
            return
        self._fx -= mx
        self._fy -= my

        tx, ty = self.px + mx, self.py + my
        nx = min(max(tx, X_MIN), X_MAX)
        ny = min(max(ty, Y_MIN), Y_MAX)
        if nx != tx:
            self._wall_ms[0] = now
            self._fx = 0.0              # don't bank turns against the edge
        if ny != ty:
            self._wall_ms[1] = now
            self._fy = 0.0
        if nx == self.px and ny == self.py:
            return
        self._line(self.px, self.py, nx, ny)
        self.px, self.py = nx, ny
        if not self._dirty:
            self._first_change = now
        self._dirty = True
        self._last_move = now

    def _maybe_refresh(self):
        if not self._dirty or not display.is_done_updating():
            return
        now = time.ticks_ms()
        if (time.ticks_diff(now, self._last_move) >= IDLE_REFRESH_MS
                or time.ticks_diff(now, self._first_change) >= MAX_WAIT_MS):
            display.update(fast=True, keep_red=True)
            self._dirty = False

    # ---------------- LEDs ----------------
    def _recent(self, t, now, ms):
        return t is not None and time.ticks_diff(now, t) < ms

    def _knob_leds(self, axis, now):
        if self._recent(self._wall_ms[axis], now, 250):
            return [WALL_COLOR] * 4
        if not self._recent(self._turn_ms[axis], now, LED_TURN_HOLD_MS):
            return None
        vals = ["off"] * 4
        vals[int(self._phase[axis]) % 4] = X_COLOR if axis == 0 else Y_COLOR
        return vals

    def _update_leds(self):
        if leds.sequence_func is not None:
            self._led_frame = None      # redraw once the blink finishes
            return
        now = time.ticks_ms()
        left = self._knob_leds(0, now)
        right = self._knob_leds(1, now)
        if left is None and right is None:
            if not display.is_done_updating():
                frame = ("cyan",) * 8
            elif self._dirty:
                frame = ("yellow",) * 8
            else:
                frame = ("green",) * 8
        else:
            frame = tuple((left or ["off"] * 4) + (right or ["off"] * 4))
        if frame != self._led_frame:
            self._led_frame = frame
            leds.show(frame)

    # ---------------- app loop ----------------
    async def arun(self):
        try:
            if self.launched:
                self._setup()
                self._blank(full=True)

            if self.shake.poll():
                print("Sketch: shake, erasing")
                leds.blink("white", times=3, on_ms=80, off_ms=80)
                self._blank(full=False)
            if UserA.pressed():
                self._blank(full=False)
            if UserB.pressed():
                if self.x_knob and not self.y_knob:
                    self.single_axis = "y" if self.single_axis == "x" else "x"
                    leds.blink("white", times=1 if self.single_axis == "x" else 2)
                else:
                    display.update()    # full refresh of the current drawing
                    self._dirty = False

            self._poll_knobs()
            self._maybe_refresh()
            self._update_leds()
        except Exception as ex:
            import sys
            print("Sketch hit an exception")
            sys.print_exception(ex)
        await aio.sleep_ms(self.sleep_ms)
