"""Fast demo: the smallest app that uses the display driver's animation session.

A square slides across the panel with a frame counter, at about 0.75 frames a second, using
nothing but lib/epd.py and MicroPython's framebuf. See docs/fast_refresh.md for how the panel
is being driven and what the limits are. Leave with Menu +/-; the app does a stock full refresh
on the way out so the panel is clean for whatever comes next.
"""

import asyncio as aio
import framebuf
import time

from apps.base_app import BaseApp
from lib.epd import anim_lut, display, display_height, display_width
from lib.led_bar import NUM_LEDS, leds


class FastDemo(BaseApp):
    def __init__(self, name=None):
        self.name = "Fast demo"
        self.sleep_ms = 100
        self.launched = False
        self.active = False
        self.last_run = 0

    def run(self):
        self.last_run = time.ticks_ms()
        if self.launched and not self.active:
            self.active = True
            aio.create_task(self._loop())
            aio.create_task(self._watch_exit())

    async def _watch_exit(self):
        # the switcher stops calling run() when the user leaves; notice within 5 s
        while self.active:
            await aio.sleep_ms(500)
            if time.ticks_diff(time.ticks_ms(), self.last_run) > 5000:
                self.active = False

    async def _loop(self):
        plane = display._framebuffer_black                 # 960 x 672, 1 bit, MSB first, 1 = black
        fb = framebuf.FrameBuffer(plane, display_width, display_height, framebuf.MONO_HLSB)
        red = display._framebuffer_red
        n = 0
        try:
            await display.wait_done()                       # let any stock refresh finish
            fb.fill(0)
            for i in range(len(red)):
                red[i] = 0                                  # second plane stays empty: black and white only
            await display.anim_begin(anim_lut())            # power up, load the fast waveform, show white
            while self.active:
                t0 = time.ticks_ms()
                fb.fill(0)
                x = 40 + (n * 24) % (display_width - 200)
                fb.fill_rect(x, display_height // 2 - 60, 120, 120, 1)
                fb.rect(20, 20, display_width - 40, display_height - 40, 1)
                fb.text("FRAME %d" % n, 40, 40, 1)
                render_ms = time.ticks_diff(time.ticks_ms(), t0)
                for i in range(NUM_LEDS):
                    leds[i] = (0, 6, 0) if i == n % NUM_LEDS else (0, 0, 0)
                leds.write()
                waited = await display.anim_frame()         # waits for the panel, sends, triggers
                n += 1
                if n % 10 == 0:
                    print("fastdemo: frame %d, render %d ms, wait+send %d ms" % (n, render_ms, waited))
        except Exception as ex:
            print("fastdemo:", ex)
        finally:
            await display.anim_end()
            self.active = False
            leds.fill((0, 0, 0))
            display.clear()
            display.update()                                # stock full refresh: clean panel for the menu
