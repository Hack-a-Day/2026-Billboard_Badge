"""Mandelbrot Fractal Viewer for 2026 Billboard Badge"""

import time
import micropython
import asyncio as aio
from apps.base_app import BaseApp
from lib.button import UserA, UserB, MenuMinus, MenuPlus, Boot
from lib.epd import display
from lib.led_bar import leds

@micropython.viper
def render_mandelbrot_chunk(fb_black: ptr8, fb_red: ptr8, max_iter: int, mode: int, d_start: int, d_count: int):
    SCALE = 8192
    FOUR = 4 * 8192 * 8192
    x_min = -17613  # -2.15 in 13-bit fixed point
    
    for step in range(d_count):
        d = d_start + step
        py_top = 335 - d
        py_bot = 336 + d
        row_top_off = py_top * 120
        row_bot_off = py_bot * 120
        
        y0 = (d * 8602) // 336
        
        for byte_idx in range(120):
            b_black = 0
            b_red = 0
            px_base = byte_idx * 8
            
            for b in range(8):
                px = px_base + b
                x0 = x_min + (px * 24576) // 960
                
                x = 0
                y = 0
                i = 0
                while i < max_iter:
                    x2 = x * x
                    y2 = y * y
                    if (x2 + y2) > FOUR:
                        break
                    y = ((x * y) >> 12) + y0
                    x = ((x2 - y2) >> 13) + x0
                    i += 1
                
                bit_mask = 1 << (7 - b)
                
                if mode == 0:
                    if i == max_iter:
                        b_black |= bit_mask
                    elif i >= 8:
                        b_red |= bit_mask
                elif mode == 1:
                    if i == max_iter:
                        b_red |= bit_mask
                    elif i >= 6:
                        b_black |= bit_mask
                elif mode == 2:
                    if i == max_iter:
                        pass
                    elif i >= 8:
                        b_red |= bit_mask
                    else:
                        b_black |= bit_mask
                else:
                    if i == max_iter:
                        b_black |= bit_mask
                    elif (i & 1) == 0 and i >= 6:
                        b_red |= bit_mask
            
            fb_black[row_top_off + byte_idx] = b_black
            fb_black[row_bot_off + byte_idx] = b_black
            fb_red[row_top_off + byte_idx] = b_red
            fb_red[row_bot_off + byte_idx] = b_red


class MandelbrotApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        self.name = "Mandelbrot"
        self.sleep_ms = 100
        self.launched = False
        self.mode = 3
        self.max_iter = 35
        self.modes_count = 4
        self.mode_names = [
            "Classic (Black heart, Red filaments)",
            "Fire & Magma (Red heart, Black filaments)",
            "Dark Mode (White heart, Red glow, Black sky)",
            "Zebra Rings (Black heart, Red/White bands)"
        ]

    async def render(self):
        print(f"[Mandelbrot] Computing mode {self.mode}: {self.mode_names[self.mode]}")
        display.clear()
        leds.clear()
        
        # Rapid blinking Blue on LED 0 while calculating math (~1.8s)
        blink_state = False
        t0 = time.ticks_ms()
        for chunk in range(14):
            d_start = chunk * 24
            render_mandelbrot_chunk(
                display._framebuffer_black,
                display._framebuffer_red,
                self.max_iter,
                self.mode,
                d_start,
                24
            )
            blink_state = not blink_state
            leds[0] = (0, 0, 50) if blink_state else (0, 0, 0)
            leds.write()
            await aio.sleep_ms(1)

        t1 = time.ticks_ms()
        print(f"[Mandelbrot] Rendered math in {time.ticks_diff(t1, t0)} ms")
        
        # Trigger generic fast hardware update in lib/epd.py (handles Viper split, 20MHz SPI, and Red/Green LEDs)
        display._display_updated.clear()
        display.update()
        await display._display_updated.wait()

    async def arun(self):
        if self.launched:
            self.launched = False
            await self.render()
            return
            
        if UserB.pressed():
            self.mode = (self.mode + 1) % self.modes_count
            print(f"[Mandelbrot] Button B pressed -> Mode {self.mode}: {self.mode_names[self.mode]}")
            await self.render()
        elif UserA.pressed():
            self.mode = (self.mode - 1) % self.modes_count
            print(f"[Mandelbrot] Button A pressed -> Mode {self.mode}: {self.mode_names[self.mode]}")
            await self.render()
            
        await aio.sleep_ms(self.sleep_ms)
