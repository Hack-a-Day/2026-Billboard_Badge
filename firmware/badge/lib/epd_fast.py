"""Fast partial refresh for the B1 panel

display.update(fast=True, pulls=n) drives only the pixels that changed since the
last frame, with the short 400-byte waveform (data/rendered_luts.py, lutb_base)
instead of the panel's own ~34 s one. Black and white only: it cannot make red.

How it works, from the iTC97 driver reference:
* Each pixel's pair of plane bits (red, bw) picks a waveform table: 00 paper,
  01 ink, 11 drives nothing. Changed pixels get 00 or 01; every other pixel is
  sent as 11, so it holds. That is the partial update.
* The panel session stays open between frames. A frame then costs the plane
  transfer plus `pulls` bare 0x15 triggers of about 1.07 s each.
* One pull is a fraction of the full dose (the vendor uses three for solid
  black). Pixels driven with fewer than FULL_PULLS stay "dirty" and are driven
  again next frame, so the dose adds up and nothing is left grey.
* A session that has been idle for IDLE_MS is shut down by the display task.
* Red that is already on the panel (drawn by the last full refresh) is held, like
  every other unchanged pixel: those pixels are sent as 11 and never driven, so a
  red banner survives the fast frames. A fast frame whose red plane differs from
  the last full refresh cannot be drawn, so it becomes one full refresh instead.
* The first fast frame after a gray frame, or at boot when the panel content is
  unknown, is also shown with one full refresh first.

BUSY handling: the ordinary driver proceeds when either COG's BUSY is high (see
the "Don't wait on busy signal from both halfs" commit), so a pin that never comes
up is tolerated there. Here, after the booster starts, only the pins that really
come ready are watched, and the transitions are printed for the first pull of each
session, to help debug a badge that behaves differently.
"""

import asyncio as aio
import micropython
from micropython import const
import time

FRAME_BYTES = const(80640)
HALF_BYTES = const(40320)
FULL_PULLS = const(3)
IDLE_MS = const(2000)
READY_MS = const(10000)
MAX_FAILURES = const(2)


@micropython.viper
def _copy(dst: ptr8, src: ptr8, count: int):
    for i in range(count):
        dst[i] = src[i]


@micropython.viper
def _fill(dst: ptr8, count: int, value: int):
    for i in range(count):
        dst[i] = value


@micropython.viper
def _same(a: ptr8, b: ptr8, count: int) -> int:
    for i in range(count):
        if a[i] != b[i]:
            return 0
    return 1


@micropython.viper
def _planes(black: ptr8, red: ptr8, prev: ptr8, dirty: ptr8, bw_out: ptr8, red_out: ptr8, keep_dirty: int):
    """Turn the wanted image into the two planes to send, and remember it.
    Changed or dirty pixels get their target: 00 paper, 01 ink. Everything else is
    11, hold, including every red pixel (red is never driven, only held)."""
    for i in range(80640):
        r = red[i]
        want = black[i] & (r ^ 255)
        drive = ((want ^ prev[i]) | dirty[i]) & (r ^ 255)
        hold = drive ^ 255
        bw_out[i] = hold | want
        red_out[i] = hold
        prev[i] = want
        if keep_dirty:
            dirty[i] = drive
        else:
            dirty[i] = 0


class _State:
    def __init__(self):
        # What the panel shows, as ink (1) over paper (0), in framebuffer order.
        self.prev = bytearray(FRAME_BYTES)
        # The red plane of the last full refresh: red on the panel, held by fast frames.
        self.red_prev = bytearray(FRAME_BYTES)
        # Pixels that have had a partial dose and are not settled yet.
        self.dirty = bytearray(FRAME_BYTES)
        # prev and red_prev match what the panel shows.
        self.valid = False
        self.open = False
        self.last_ms = 0
        self.waveform = None
        # BUSY pins to watch (those that came ready), and consecutive failures.
        self.pins = None
        self.live = None
        self.failures = 0
        self.traced = 0
        # Scratch, allocated on first fast frame.
        self.bw = None
        self.red = None
        self.halves = None


def state(display):
    st = display._fast
    if st is None:
        st = display._fast = _State()
    return st


def _scratch(st):
    if st.bw is None:
        st.bw = bytearray(FRAME_BYTES)
        st.red = bytearray(FRAME_BYTES)
        st.halves = tuple(bytearray(HALF_BYTES) for _ in range(4))


def _split(src, left, right):
    from lib.epd import _fast_split_and_reverse, _REVERSE_LUT
    _fast_split_and_reverse(src, left, right, _REVERSE_LUT)


async def leave(display, inexact):
    """Called by the display task before an ordinary refresh: record what it is
    about to show, and close the fast session. inexact: gray, so prev is unknown."""
    st = state(display)
    st.valid = not inexact
    if st.valid:
        _copy(st.prev, display._framebuffer_black, FRAME_BYTES)
        _copy(st.red_prev, display._framebuffer_red, FRAME_BYTES)
    _fill(st.dirty, FRAME_BYTES, 0)
    if st.open:
        await close(display)


class _Trace:
    """BUSY pin transitions as (ms, BUSY1, BUSY2), for debugging."""

    def __init__(self, pins):
        self.pins = pins
        self.t0 = time.ticks_ms()
        self.last = None
        self.events = []

    def sample(self):
        now = tuple(p.value() for p in self.pins)
        if now != self.last and len(self.events) < 24:
            self.events.append((time.ticks_diff(time.ticks_ms(), self.t0),) + now)
        self.last = now


async def _wait_high(pins, timeout_ms, trace=None):
    start = time.ticks_ms()
    while True:
        if trace:
            trace.sample()
        if all(p.value() for p in pins):
            return True
        if time.ticks_diff(time.ticks_ms(), start) >= timeout_ms:
            return False
        await aio.sleep_ms(5)


async def _learn_pins(st, trace):
    """After the booster starts: wait for BUSY, then watch only pins that came up."""
    pins = st.pins
    start = time.ticks_ms()
    while not any(p.value() for p in pins):
        trace.sample()
        if time.ticks_diff(time.ticks_ms(), start) >= READY_MS:
            raise RuntimeError("BUSY never came ready after the booster started")
        await aio.sleep_ms(5)
    await _wait_high(pins, 2000, trace)
    st.live = tuple(p for p in pins if p.value())
    print("EPD fast: BUSY ready after", time.ticks_diff(time.ticks_ms(), start),
          "ms, watching", [i + 1 for i, p in enumerate(pins) if p.value()])


async def _pull(display, st, trace):
    live = st.live
    if not await _wait_high(live, READY_MS, trace):
        raise RuntimeError("BUSY not ready before pull")
    display._send_index_data(0x15, [0x3C])
    # Seen low, per pin: poll without yielding so a short pulse cannot be missed.
    start = time.ticks_ms()
    seen = [False] * len(live)
    while not all(seen):
        for i in range(len(live)):
            if not live[i].value():
                seen[i] = True
        trace.sample()
        if time.ticks_diff(time.ticks_ms(), start) >= 100:
            raise RuntimeError("BUSY never went low after the trigger")
        time.sleep_ms(1)
    if not await _wait_high(live, READY_MS, trace):
        raise RuntimeError("BUSY did not come back after the trigger")


async def _shutdown(display, st):
    from lib.epd import EPD_DC_PIN, EPD_CS1_PIN, EPD_RST_PIN
    # Power down whether or not BUSY cooperates: waits are bounded and not fatal.
    pins = st.live or ()
    for step in range(2):
        if pins:
            await _wait_high(pins, READY_MS)
        else:
            start = time.ticks_ms()
            while (st.pins and not any(p.value() for p in st.pins)
                   and time.ticks_diff(time.ticks_ms(), start) < READY_MS):
                await aio.sleep_ms(5)
        if step == 0:
            display._send_index_data(0x09, [0x7F])
            display._send_index_data(0x05, [0x7D])
            display._send_index_data(0x09, [0x00])
            await aio.sleep_ms(200)
    EPD_DC_PIN.value(False)
    EPD_CS1_PIN.value(False)
    EPD_RST_PIN.value(False)
    EPD_CS1_PIN.value(True)


async def close(display):
    st = state(display)
    st.open = False
    try:
        await aio.wait_for(_shutdown(display, st), 30)
    except Exception as ex:
        print("EPD fast: shutdown failed:", ex)
        display._panel_monochrome = False
        st.valid = False


async def idle_check(display):
    st = state(display)
    if st.open and time.ticks_diff(time.ticks_ms(), st.last_ms) > IDLE_MS:
        await close(display)


async def _promote(display, st):
    """One ordinary full refresh of the frame, red included, so the panel shows it
    and its content is known."""
    print("EPD fast: showing this frame with a full refresh")
    if st.open:
        await close(display)
    _scratch(st)
    bw_left, red_left, bw_right, red_right = st.halves
    _split(display._framebuffer_black, bw_left, bw_right)
    _split(display._framebuffer_red, red_left, red_right)
    _copy(st.prev, display._framebuffer_black, FRAME_BYTES)
    _copy(st.red_prev, display._framebuffer_red, FRAME_BYTES)
    _fill(st.dirty, FRAME_BYTES, 0)
    old = display._reverse_fb_bits
    display._reverse_fb_bits = False
    try:
        await display.global_update(bw_left, red_left, bw_right, red_right)
    finally:
        display._reverse_fb_bits = old
    st.valid = True


async def _open(display, st, halves):
    from data import rendered_luts
    if st.waveform is None:
        table = bytes(rendered_luts.lutb_base)
        if len(table) != 400:
            raise ValueError("Fast refresh requires the 400-byte B1 waveform")
        st.waveform = table
    if st.pins is None:
        from lib.epd import EPD_BUSY1_PIN, EPD_BUSY2_PIN
        st.pins = (EPD_BUSY1_PIN, EPD_BUSY2_PIN)
    await display._reset()
    display._send_images(*halves)
    await display.cog_initial()
    display._send_index_data(0xB0, st.waveform)
    await display._dcdc_soft_start()
    st.open = True
    st.traced = 0
    trace = _Trace(st.pins)
    try:
        await _learn_pins(st, trace)
    except Exception:
        print("EPD fast: BUSY", trace.events)
        raise


async def fast_update(display, pulls):
    st = state(display)
    trace = None
    try:
        if not st.valid or not _same(display._framebuffer_red, st.red_prev, FRAME_BYTES):
            await _promote(display, st)
            return
        _scratch(st)
        _planes(display._framebuffer_black, display._framebuffer_red, st.prev, st.dirty,
                st.bw, st.red, 1 if pulls < FULL_PULLS else 0)
        bw_left, red_left, bw_right, red_right = st.halves
        _split(st.bw, bw_left, bw_right)
        _split(st.red, red_left, red_right)
        old = display._reverse_fb_bits
        display._reverse_fb_bits = False  # Already reversed above.
        try:
            if st.open:
                display._send_images(bw_left, red_left, bw_right, red_right)
            else:
                await _open(display, st, st.halves)
        finally:
            display._reverse_fb_bits = old
        for n in range(pulls):
            trace = _Trace(st.pins)
            t0 = time.ticks_ms()
            await _pull(display, st, trace)
            if st.traced < 2:
                st.traced += 1
                print("EPD fast: pull took", time.ticks_diff(time.ticks_ms(), t0),
                      "ms, BUSY (ms, 1, 2):", trace.events)
        st.last_ms = time.ticks_ms()
        st.failures = 0
    except Exception as ex:
        # Keep the display task alive. The next fast frame starts over with a
        # full refresh, since nothing is known about the panel now.
        print("EPD fast: update failed:", ex)
        if trace is not None:
            print("EPD fast: BUSY (ms, 1, 2):", trace.events)
        st.failures += 1
        display._panel_monochrome = False
        st.valid = False
        if st.open:
            await close(display)
