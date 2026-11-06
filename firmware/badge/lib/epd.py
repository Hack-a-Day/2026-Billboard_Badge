"""E-Paper Display Driver for Billboard Badge

Ported from Wiring in https://github.com/PervasiveDisplays/EPD_Driver_GU_large/blob/e408154652802a82e520cc25ba04adeb347d0528/src/EPD_Driver.cpp

Refresh modes
-------------
display.update()            Full refresh on the panel's own (OTP) waveform.
                            Best quality and the only one that makes a good
                            red. ~35 s on the E2969JS0B1.
display.update(fast=True)   Short waveform from data/rendered_luts.py, written
                            to register 0xB0 (the same tables and sequence as
                            firmware/esp32s3-itc97). Black and white only: red
                            comes up pale and fades a little with every fast
                            frame, so use a full refresh for red content.
                            On driver B the panel runs each refresh for a fixed
                            ~1 s, so a fast update re-triggers it FAST_PULLS_B
                            times without resending anything.
display.update(fast=True, keep_red=True)
                            Fast refresh that leaves red pixels alone: the red
                            table is replaced with idle groups, so red already
                            on the panel (from a full refresh) is not touched.
                            For apps that draw black over a red/white layout.

--COPYRIGHT--
Brief The drivers and functions of development board
Copyright (c) 2012-2021 Pervasive Displays Inc. All rights reserved.
 Authors: Pervasive Displays Inc.
 Redistribution and use in source and binary forms, with or without
 modification, are permitted provided that the following conditions
 are met:
 1. Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.
 2. Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in
    the documentation and/or other materials provided with the
    distribution.
 THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import asyncio as aio
import btree
import framebuf
import gc
import micropython
from machine import Pin, SPI, SoftSPI
from micropython import const
import time

import lvgl

from lib.large_png import show_png

# Image data goes out over hardware SPI at the PDi reference driver's 8 MHz.
# Set False to fall back to the bit-banged SoftSPI.
USE_HW_SPI = True
SPI_BAUD = 8_000_000
# Fast refresh: how many times to re-trigger the latched waveform per update.
# Driver B runs a fixed ~1 s per trigger, so one pull can't clear the previous
# image; on driver 8 the time follows the table and repeating only adds time.
FAST_PULLS_B = 3
FAST_PULLS_8 = 1
_SPI_CHUNK = const(4096)   # bytes per SPI write before yielding to other tasks
_BUSY_POLL_MS = const(10)

EPD_CS1_PIN = Pin(13, mode=Pin.OUT)
EPD_CS2_PIN = Pin(3, mode=Pin.OUT)
EPD_BUSY1_PIN = Pin(12, mode=Pin.IN)
EPD_BUSY2_PIN = Pin(8, mode=Pin.IN)
EPD_DC_PIN = Pin(10, mode=Pin.OUT)  # Data High Command Low
EPD_RST_PIN = Pin(11, mode=Pin.OUT)
EPD_SCL_PIN = Pin(21, mode=Pin.OUT)
EPD_SDA_PIN = Pin(47, mode=Pin.OUT)
EPD_SPI_MISO_PIN = Pin(45, mode=Pin.IN)  # Not connected, signal not needed
EPD_PWR_EN_PIN = 48


pdi_cp = 0x9608
pdi_size = 0x96  # 9.69"
display_width = const(960)
half_width = const(display_width // 2)
display_height = const(672)
# Frame-buffer size = width * height / 8 * depth, uint32_t
# image_data_size is really only half the panel pixels, of one color
half_frame_one_color_size = const(display_height * display_width // (8 * 2))
# The width of rows in the full-frame and half-frame framebuffers
# Counting bytes, not pixels, since each byte is 8 packed pixels
display_width_bytes = const(display_width // 8)
display_half_width_bytes = const(display_width_bytes // 2)


# Two displays use different bit orders
def reverse_byte(byte: int) -> int:
    byte = (byte & 0xF0) >> 4 | (byte & 0x0F) << 4
    byte = (byte & 0xCC) >> 2 | (byte & 0x33) << 2
    byte = (byte & 0xAA) >> 1 | (byte & 0x55) << 1
    return byte


_REVERSE_LUT = bytes(reverse_byte(b) for b in range(256))
_IDENTITY_LUT = bytes(range(256))


@micropython.viper
def _split_halves(src, left, right, lut):
    """Split a 120-byte-wide plane into two 60-byte-wide halves (one per
    CoG), mapping every byte through `lut` (bit reversal or identity)."""
    s = ptr8(src)
    l = ptr8(left)
    r = ptr8(right)
    t = ptr8(lut)
    row = 0
    while row < 672:
        sb = row * 120
        db = row * 60
        c = 0
        while c < 60:
            l[db + c] = t[s[sb + c]]
            r[db + c] = t[s[sb + 60 + c]]
            c += 1
        row += 1


@micropython.viper
def _rgb565_to_planes(src, stride: int, w: int, h: int, x1: int, y1: int, black, red):
    """Convert an LVGL RGB565 area into the black/red planes, using the same
    color rule as before (top bit of each channel)."""
    s = ptr8(src)
    bk = ptr8(black)
    rd = ptr8(red)
    row = 0
    while row < h:
        base = (y1 + row) * 960 + x1
        ro = row * stride
        col = 0
        while col < w:
            i = ro + col * 2
            c = s[i] | (s[i + 1] << 8)  # RGB565, little-endian
            idx = base + col
            byte = idx >> 3
            mask = 128 >> (idx & 7)
            clear = mask ^ 255
            r_hi = c & 0x8000
            g_hi = c & 0x0400
            b_hi = c & 0x0010
            if r_hi and g_hi == 0 and b_hi == 0:
                rd[byte] = rd[byte] | mask
                bk[byte] = bk[byte] & clear
            elif r_hi == 0 and g_hi == 0 and b_hi == 0:
                bk[byte] = bk[byte] | mask
                rd[byte] = rd[byte] & clear
            else:
                bk[byte] = bk[byte] & clear
                rd[byte] = rd[byte] & clear
            col += 1
        row += 1


def _keep_red_lut(lut, group_size, groups):
    """Copy of a fast LUT with the red tables (2 and 3) replaced by idle
    groups, so red pixels get no drive. Table index is (red bit << 1) | bw bit;
    the idle group is the table's own last VCOM group (ground, one frame)."""
    b = bytearray(lut)
    idle_at = (4 * groups + groups - 1) * group_size
    idle = bytes(b[idle_at:idle_at + group_size])
    for t in (2, 3):
        for g in range(groups):
            o = (t * groups + g) * group_size
            b[o:o + group_size] = idle
    return bytes(b)


def _busy() -> bool:
    """True while either CoG is busy (BUSY is active low)."""
    return not EPD_BUSY1_PIN.value() or not EPD_BUSY2_PIN.value()


async def _wait_not_busy(timeout_ms=180_000):
    t0 = time.ticks_ms()
    while _busy():
        if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
            print("EPD: BUSY timeout")
            return
        await aio.sleep_ms(_BUSY_POLL_MS)


def _make_spi():
    if USE_HW_SPI:
        try:
            return SPI(1, baudrate=SPI_BAUD, polarity=0, phase=0,
                       sck=EPD_SCL_PIN, mosi=EPD_SDA_PIN, miso=EPD_SPI_MISO_PIN)
        except Exception as ex:
            print(f"EPD: hardware SPI failed ({ex}), using SoftSPI")
    spi = SoftSPI(
        baudrate=SPI_BAUD, sck=EPD_SCL_PIN, mosi=EPD_SDA_PIN, miso=EPD_SPI_MISO_PIN
    )
    spi.init()
    return spi


class EPaperDisplay:
    width = display_width
    height = display_height

    def __init__(self) -> None:
        print("Initializing E-Paper Display")
        EPD_DC_PIN.value(True)
        EPD_RST_PIN.value(True)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)
        self.spi = _make_spi()

        display_pn = b"B1"
        try:
            with open("/data/hwconf", "rb") as f:
                hwconf = btree.open(f)
                display_pn = hwconf.get(b"display_pn", b"B1")
        except OSError:
            pass
        self._reverse_fb_bits = display_pn != b"84"
        # E2969JS084 = "driver 8", E2969JS0B1 = "driver B" (see rendered_luts.py)
        self._driver_b = display_pn != b"84"
        self._fast_luts = None  # loaded on the first fast update

        # The framebuffers are set and display should be updated.
        self._framebuffer_ready = aio.Event()
        self._request = (False, None, False)  # fast, passes, keep_red

        """Each byte is a mini-row of 8 bits for pixels of one color (1 sets the color, 0 is white)
        Bytes are in rows across the screen, as seen on the badge
        0 ------------------------> 119 [120 bytes per row]
        120 ---------------------> 239
        ...                        ...
        [672 rows of 120 bytes per row]
        """
        self._framebuffer_black = bytearray(half_frame_one_color_size * 2)
        self._framebuffer_red = bytearray(half_frame_one_color_size * 2)
        # Framebuffers that can be drawn to directly
        self.black = framebuf.FrameBuffer(
            self._framebuffer_black, display_width, display_height, framebuf.MONO_HLSB
        )
        self.red = framebuf.FrameBuffer(
            self._framebuffer_red, display_width, display_height, framebuf.MONO_HLSB
        )

        # Is the display done updating? Start set since the display is not updating before boot.
        self._display_updated = aio.Event()
        self._display_updated.set()
        gc.collect()

        # Set up lvgl
        lvgl.init()
        # Give LVGL a buffer 2-bytes-per-pixel for RGB565 format
        self._lv_draw_buf = bytearray(display_width * display_height * 2)

        self.lv_display = lvgl.display_create(display_width, display_height)
        self.lv_display.set_color_format(lvgl.COLOR_FORMAT.RGB565)
        self.lv_display.set_buffers(
            self._lv_draw_buf, None, len(self._lv_draw_buf),
            lvgl.DISPLAY_RENDER_MODE.PARTIAL,
        )
        self.lv_display.set_flush_cb(self._lv_flush)
        self.lv_screen = lvgl.screen_active()
        self.lv_screen.set_size(display_width, display_height)

    def _lv_flush(self, disp, area, color_p):
        x1, y1 = area.x1, area.y1
        w = area.x2 - x1 + 1
        h = area.y2 - y1 + 1
        try:
            stride = lvgl.draw_buf_width_to_stride(w, lvgl.COLOR_FORMAT.RGB565)
        except AttributeError:
            stride = w * 2
        src = color_p.__dereference__(stride * h)
        try:
            _rgb565_to_planes(src, stride, w, h, x1, y1,
                              self._framebuffer_black, self._framebuffer_red)
        except TypeError:
            # In case this LVGL build returns something viper can't point at
            _rgb565_to_planes(bytes(src), stride, w, h, x1, y1,
                              self._framebuffer_black, self._framebuffer_red)
        disp.flush_ready()

    def update(self, fast=False, passes=None, keep_red=False):
        """Done updating the framebuffer, update the display with the contents.

        Returns right away; the refresh runs in the background task. See the
        module docstring for the modes. `passes` overrides the number of fast
        pulls for this update.
        """
        self._request = (fast, passes, keep_red)
        self._display_updated.clear()
        self._framebuffer_ready.set()

    def clear(self):
        """Clear framebuffers to full white"""
        self.black.fill(0)
        self.red.fill(0)

    def is_done_updating(self):
        """Is the display done updating?"""
        return self._display_updated.is_set()

    async def wait_done(self):
        """await display.wait_done() to wait for the refresh to finish."""
        await self._display_updated.wait()

    def draw_image(self, image, height: int, width: int, row: int = 0, col: int = 0):
        for img_r in range(height):
            for img_c in range(width):
                color = image[img_r][img_c]
                red = color[0] > 127
                black = color[0] < 128 and color[1] < 128 and color[2] < 128
                self.black.pixel((col + img_c), (row + img_r), int(black))
                self.red.pixel((col + img_c), (row + img_r), int(red))

    def draw_png(self, filename: str, row: int = 0, col: int = 0):

        gc.collect()
        print(f"Loading {filename} This will take a while... {time.time()}")
        show_png(filename, self._framebuffer_black, self._framebuffer_red, row, col)
        print("Done loading png")
        gc.collect()

    async def run(self):
        black_left = bytearray(half_frame_one_color_size)
        black_right = bytearray(half_frame_one_color_size)
        red_left = bytearray(half_frame_one_color_size)
        red_right = bytearray(half_frame_one_color_size)
        lut = _REVERSE_LUT if self._reverse_fb_bits else _IDENTITY_LUT
        while True:
            await self._framebuffer_ready.wait()
            # Clear before copying: an update() requested while this refresh
            # runs then triggers another one instead of being dropped.
            self._framebuffer_ready.clear()
            fast, passes, keep_red = self._request
            _split_halves(self._framebuffer_black, black_left, black_right, lut)
            _split_halves(self._framebuffer_red, red_left, red_right, lut)
            try:
                await self.global_update(black_left, red_left, black_right, red_right,
                                         fast, passes, keep_red)
            except Exception as ex:
                import sys
                print("EPD: update failed")
                sys.print_exception(ex)
            gc.collect()
            if not self._framebuffer_ready.is_set():
                self._display_updated.set()

    async def global_update(
        self,
        black_left: bytearray | bytes | memoryview[int],
        red_left: bytearray | bytes | memoryview[int],
        black_right: bytearray | bytes | memoryview[int],
        red_right: bytearray | bytes | memoryview[int],
        fast=False,
        passes=None,
        keep_red=False,
    ):
        tick = time.ticks_ms()
        wave = self._fast_lut(keep_red) if fast else None
        if wave is None:
            passes = 1
        elif passes is None:
            passes = FAST_PULLS_B if self._driver_b else FAST_PULLS_8

        await self._reset()
        await self._send_images(black_left, red_left, black_right, red_right)
        await self.cog_initial()
        if wave is not None:
            # After the 0x44/0x45/0xA7 sequence (which loads the OTP waveform
            # into these registers) and before the DC/DC soft start.
            self._send_index_data(0xB0, wave)
        await self._dcdc_soft_start()
        for _ in range(max(1, passes)):
            # Later passes re-trigger the latched waveform; nothing is resent.
            await self._display_refresh()
        await self._dcdc_soft_shutdown()
        mode = ("fast x%d%s" % (passes, ", keep red" if keep_red else "")) if wave else "full"
        print(f"EPD: {mode} update done in {time.ticks_diff(time.ticks_ms(), tick)} ms")

    def _fast_lut(self, keep_red):
        if self._fast_luts is None:
            try:
                import data.rendered_luts as L
                if self._driver_b:
                    base = bytes(L.lutb_base)
                    self._fast_luts = (base, _keep_red_lut(base, 8, 10))
                else:
                    base = bytes(L.lut8_nored_clear)
                    self._fast_luts = (base, _keep_red_lut(base, 4, 9))
                del L
                gc.collect()
            except Exception as ex:
                print(f"EPD: no fast waveform ({ex}), using a full refresh")
                self._fast_luts = ()
        if not self._fast_luts:
            return None
        return self._fast_luts[1 if keep_red else 0]

    async def cog_initial(self):
        self._send_index_data(0x05, [0x7D])
        await aio.sleep_ms(200)
        self._send_index_data(0x05, [0x00])
        await aio.sleep_ms(10)
        self._send_index_data(0xD8, [0x80])  # MS_SYNC)
        self._send_index_data(0xD6, [0x00])  # BVSS
        self._send_index_data(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data(0xA7, [0x00])
        await aio.sleep_ms(100)
        self._send_index_data(0x03, [0x00, 0x11])  # OSC

        # Display 1
        self._send_index_data_left(0x44, [0x00])
        self._send_index_data_left(0x45, [0x80])
        self._send_index_data_left(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data_left(0xA7, [0x00])
        await aio.sleep_ms(100)
        self._send_index_data_left(0x44, [0x06])
        self._send_index_data_left(0x45, [0x82])  # Temperature 0x82@25C
        self._send_index_data_left(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data_left(0xA7, [0x00])
        await aio.sleep_ms(100)

        # Display 2
        self._send_index_data_right(0x44, [0x00])
        self._send_index_data_right(0x45, [0x80])
        self._send_index_data_right(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data_right(0xA7, [0x00])
        await aio.sleep_ms(100)
        self._send_index_data_right(0x44, [0x06])
        self._send_index_data_right(0x45, [0x82])  # Temperature 0x82@25C
        self._send_index_data_right(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data_right(0xA7, [0x00])
        await aio.sleep_ms(100)

        self._send_index_data(0x60, [0x25])  # TCON
        self._send_index_data_left(0x61, [0x01])  # STV_DIR for Display 1
        self._send_index_data(0x02, [0x00])  # VCOM

    async def cog_poweroff(self):
        """CoG shutdown function
        Shuts down the CoG and DC/DC circuit after all update functions
        """
        self._send_index_data(0x09, [0x7F, 0x7D, 0x00])
        await aio.sleep_ms(200)
        await _wait_not_busy()
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        await aio.sleep_ms(150)
        EPD_RST_PIN.value(False)

    def _send_index_data(self, index: int, data: bytes | list):
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        EPD_CS2_PIN.value(False)
        self.spi.write(bytes([index]))
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)
        EPD_DC_PIN.value(True)
        EPD_CS1_PIN.value(False)
        EPD_CS2_PIN.value(False)
        self.spi.write(bytes(data))
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)

    def _send_index_data_left(self, index: int, data: bytes | list | memoryview[int]):
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        EPD_CS2_PIN.value(True)
        self.spi.write(bytes([index]))
        EPD_CS1_PIN.value(True)
        EPD_DC_PIN.value(True)
        EPD_CS1_PIN.value(False)
        self.spi.write(bytes(data))
        EPD_CS1_PIN.value(True)

    def _send_index_data_right(self, index: int, data: bytes | list | memoryview[int]):
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(False)
        self.spi.write(bytes([index]))
        EPD_CS2_PIN.value(True)
        EPD_DC_PIN.value(True)
        EPD_CS2_PIN.value(False)
        self.spi.write(bytes(data))
        EPD_CS2_PIN.value(True)

    async def _send_plane(self, cs, other_cs, index: int, plane):
        """Send one half-plane to one CoG in chunks, letting the other asyncio
        tasks (LEDs, buttons) run in between. CS stays low throughout."""
        EPD_DC_PIN.value(False)
        cs.value(False)
        other_cs.value(True)
        self.spi.write(bytes([index]))
        cs.value(True)
        EPD_DC_PIN.value(True)
        cs.value(False)
        mv = memoryview(plane)
        for i in range(0, len(mv), _SPI_CHUNK):
            self.spi.write(mv[i:i + _SPI_CHUNK])
            await aio.sleep_ms(0)
        cs.value(True)

    async def _display_refresh(self):
        await _wait_not_busy()
        self._send_index_data(0x15, [0x3C])
        await aio.sleep_ms(5)

    async def _reset(self):
        """CoG driver power-on hard reset"""
        await aio.sleep_ms(200)
        EPD_RST_PIN.value(True)
        await aio.sleep_ms(20)
        EPD_RST_PIN.value(False)
        await aio.sleep_ms(200)
        EPD_RST_PIN.value(True)
        await aio.sleep_ms(200)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)
        await aio.sleep_ms(5)

    async def _send_images(
        self,
        black_left: bytearray | bytes | memoryview[int],
        red_left: bytearray | bytes | memoryview[int],
        black_right: bytearray | bytes | memoryview[int],
        red_right: bytearray | bytes | memoryview[int],
    ):
        assert len(black_left) == half_frame_one_color_size
        assert len(red_left) == half_frame_one_color_size
        assert len(black_right) == half_frame_one_color_size
        assert len(red_right) == half_frame_one_color_size

        # DUW for both left and right halves
        self._send_index_data(0x13, [0x00, 0x3B, 0x00, 0x00, 0x9F, 0x02])
        # DRFW for Both left and right halves
        self._send_index_data(0x90, [0x00, 0x3B, 0x00, 0xA9])
        ram_rw_data = [0x3B, 0x00, 0x14]
        left, right = EPD_CS1_PIN, EPD_CS2_PIN
        self._send_index_data_left(0x12, ram_rw_data)  # RAM_RW for left half
        await self._send_plane(left, right, 0x10, black_left)  # Black frame for left half
        self._send_index_data_left(0x12, ram_rw_data)  # RAM_RW for left half
        await self._send_plane(left, right, 0x11, red_left)  # Red frame for left half
        self._send_index_data_right(0x12, ram_rw_data)  # RAM_RW for right half
        await self._send_plane(right, left, 0x10, black_right)  # black frame for right half
        self._send_index_data_right(0x12, ram_rw_data)  # RAM_RW for right half
        await self._send_plane(right, left, 0x11, red_right)  # Red frame for right half

    async def _dcdc_soft_start(self):
        """DC-DC soft-start command
        Implemented after image data are uploaded to CoG
        Specific to mid-sized EPDs only
        """
        self._send_index_data(0x51, [0x50, 0x01])

        for val in range(1, 5):
            self._send_index_data(0x09, [0x1F])
            self._send_index_data(0x51, [0x50, val])
            self._send_index_data(0x09, [0x9F])
            await aio.sleep_ms(2)

        for val in range(1, 11):
            self._send_index_data(0x09, [0x1F])
            self._send_index_data(0x51, [0x0A, val])
            self._send_index_data(0x09, [0x9F])
            await aio.sleep_ms(2)

        for val in range(3, 11):
            self._send_index_data(0x09, [0x7F])
            self._send_index_data(0x51, [0x0A, val])
            self._send_index_data(0x09, [0xFF])
            await aio.sleep_ms(2)

        for val in range(9, 1, -1):
            self._send_index_data(0x09, [0x7F])
            self._send_index_data(0x51, [val, 0x0A])
            self._send_index_data(0x09, [0xFF])
            await aio.sleep_ms(2)

        self._send_index_data(0x09, [0xFF])
        await aio.sleep_ms(10)

    async def _dcdc_soft_shutdown(self):
        """DC-DC soft-shutdown command
        Implemented after image data are uploaded to CoG
        Specific to mid-sized EPDs only
        """
        await _wait_not_busy()
        self._send_index_data(0x09, [0x7F])
        self._send_index_data(0x05, [0x7D])
        self._send_index_data(0x09, [0x00])
        await aio.sleep_ms(200)
        await _wait_not_busy()
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        EPD_RST_PIN.value(False)
        EPD_CS1_PIN.value(True)


display = EPaperDisplay()
