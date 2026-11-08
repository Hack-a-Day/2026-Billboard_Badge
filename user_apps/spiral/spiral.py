"""Spiral Screensaver App — Sub-second 4-rail partial-refresh animation for the 2026 Supercon Badge.

Drives a 48x48-pixel rectangular spiral from the outer perimeter to the center of the 960x672
Spectra (Black/White/Red) e-paper panel at ~650 ms per step by keeping the dual-COG high-voltage
session open and programming a custom 20-frame, 4-rail LUT into register 0xB0.

Credits & References:
  - TheESTest/Supercon_2026_Badge_Firmware (badge_files/lib/epd_fast.py): MicroPython fast-refresh
    session architecture, make_lut() waveform builder, and Driver-B (0x45 = 0x84) OTP sequence.
  - phthallo/supercon (firmware/lib/epd.py, firmware/lib/luts.py): 20-frame 0xB0 LUT group structure
    (off = 0, 2-frame minimum group timing) and persistent-booster partial refresh technique.
  - Hack-a-Day/2026-Billboard_Badge (firmware/esp32s3-itc97/src/epd97.cpp, commit 6ba6dd8) &
    PervasiveDisplays/EPD_Driver_GU_large: Official C++ fast-mode driver and lutb_base.
  - @trandi (PR #4) & @FacioErgoSum (PR #3): 20 MHz hardware SPI(1) and @micropython.viper
    single-pass dual-COG plane split + 256-byte LUT bit-reversal.

Controls:
  User A: Toggle between Fading-Tail Snake mode (default) and Solid-Fill mode
  User B: Restart spiral from the top-left corner
"""

import asyncio as aio
import time
import micropython
from machine import Pin, SPI
from micropython import const

from apps.base_app import BaseApp
from data import rendered_luts
from lib.button import Boot, MenuMinus, MenuPlus, UserA, UserB
from lib.epd import (
    EPD_BUSY1_PIN,
    EPD_BUSY2_PIN,
    EPD_CS1_PIN,
    EPD_CS2_PIN,
    EPD_DC_PIN,
    EPD_RST_PIN,
    EPD_SCL_PIN,
    EPD_SDA_PIN,
    EPD_SPI_MISO_PIN,
    display,
    display_height,
    display_width,
    reverse_byte,
)
from lib.led_bar import NUM_LEDS, leds

# 960 x 672 divided into 48x48 pixel tiles (48 px = 6 bytes wide, byte-aligned in the 120-byte row stride)
_TILE_PX = const(48)
_TILE_BYTES = const(6)   # 48 // 8
_GRID_W = const(20)      # 960 // 48
_GRID_H = const(14)      # 672 // 48
_TOTAL_TILES = const(280)
_HALF_PLANE = const(40320)

# Number of 48x48 tiles advanced per refresh step (4 tiles = 70 steps per pass)
_TILES_PER_STEP = const(4)

# 20 frames per step (~580 ms panel drive + ~70 ms SPI = ~0.65 s per step)
_STEP_FRAMES = const(20)

_COLOR_BLACK = const(0)
_COLOR_RED = const(1)
_COLOR_WHITE = const(2)

_GND = const(0)
_VDH = const(1)   # +15 V: drive towards black
_VDL = const(2)   # -15 V: drive towards white
_VDHR = const(3)  # +7 V:  drive towards red

_REVERSE_LUT = bytearray(reverse_byte(i) for i in range(256))


def _build_spiral_path() -> list[tuple[int, int]]:
    """Generate the 280 (col, row) tiles spiraling clockwise from the outer perimeter to the center."""
    path = []
    left, right = 0, _GRID_W - 1
    top, bottom = 0, _GRID_H - 1
    while left <= right and top <= bottom:
        for c in range(left, right + 1):
            path.append((c, top))
        top += 1
        for r in range(top, bottom + 1):
            path.append((right, r))
        right -= 1
        if top <= bottom:
            for c in range(right, left - 1, -1):
                path.append((c, bottom))
            bottom -= 1
        if left <= right:
            for r in range(bottom, top - 1, -1):
                path.append((left, r))
            left += 1
    return path


_SPIRAL_PATH = _build_spiral_path()


def _make_4rail_lut(frames: int) -> bytes:
    """Build a 400-byte Driver-B LUT exposing all 4 physical voltage rails per pixel via (rd_bit << 1) | bw_bit:
      * Table 0 (00): GND  (0 V   — coast / ink-wash decay via gate-drain kickback)
      * Table 1 (01): VDH  (+15 V — drive/hold pixel on BLACK rail)
      * Table 2 (10): VDL  (-15 V — drive/hold pixel on WHITE rail)
      * Table 3 (11): VDHR (+7 V  — drive/hold pixel on RED rail)
    Group 9 Phase B is grounded across all tables so pixel storage capacitors discharge cleanly after each pass.
    """
    frames = max(20, frames)
    out = bytearray(400)
    for g in range(10):
        n = frames // 10 + (1 if g < frames % 10 else 0)
        on1 = (n + 1) // 2
        on2 = n - on1
        for t in range(5):
            if t == 1:
                a, b = (_VDH, _GND) if g == 9 else (_VDH, _VDH)
            elif t == 2:
                a, b = (_VDL, _GND) if g == 9 else (_VDL, _VDL)
            elif t == 3:
                a, b = (_VDHR, _GND) if g == 9 else (_VDHR, _VDHR)
            else:
                a, b = (_GND, _GND)
            q = (t * 10 + g) * 8
            out[q : q + 8] = bytes(((a << 6) | (a << 4) | (b << 2) | b, on1, 0, 1, on2, 0, 1, 1))
    return bytes(out)


def _make_clear_lut() -> bytes:
    """Ground Group 9 Phase B on the stock lutb_base fast-clear table."""
    lut = bytearray(rendered_luts.lutb_base)
    for t in range(4):
        idx = (t * 10 + 9) * 8
        lut[idx] &= 0xF0
    return bytes(lut)


@micropython.viper
def _fill_buf(buf: ptr8, n: int, val: int):
    i = 0
    while i < n:
        buf[i] = val
        i += 1


@micropython.viper
def _paint_tile(bw: ptr8, rd: ptr8, col_row: int, table_code: int):
    """Set all 48x48 pixels of tile (col, row) to table_code (0=GND, 1=Black, 2=White, 3=Red)."""
    col = col_row >> 8
    row = col_row & 0xFF
    b_val = 0xFF if (table_code & 1) else 0x00
    r_val = 0xFF if (table_code & 2) else 0x00
    x_byte = col * 6
    y_start = row * 48
    y = 0
    while y < 48:
        off = (y_start + y) * 120 + x_byte
        bw[off] = b_val
        bw[off + 1] = b_val
        bw[off + 2] = b_val
        bw[off + 3] = b_val
        bw[off + 4] = b_val
        bw[off + 5] = b_val
        rd[off] = r_val
        rd[off + 1] = r_val
        rd[off + 2] = r_val
        rd[off + 3] = r_val
        rd[off + 4] = r_val
        rd[off + 5] = r_val
        y += 1


@micropython.viper
def _split_and_reverse(src: ptr8, left: ptr8, right: ptr8, rev_lut: ptr8, do_rev: int):
    """Split a 960x672 plane (120 bytes/row) into Master/Slave 480x672 half-planes (60 bytes/row)."""
    r = 0
    while r < 672:
        s_row = r * 120
        d_row = r * 60
        c = 0
        if do_rev:
            while c < 60:
                left[d_row + c] = rev_lut[src[s_row + c]]
                right[d_row + c] = rev_lut[src[s_row + 60 + c]]
                c += 1
        else:
            while c < 60:
                left[d_row + c] = src[s_row + c]
                right[d_row + c] = src[s_row + 60 + c]
                c += 1
        r += 1


class SpiralApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        super().__init__(name or "Spiral")
        self.sleep_ms = 10
        self.launched = False

        # solid_mode=False (default): Fading-tail snake mode (untouched tiles coast at 0 V GND)
        # solid_mode=True (UserA):    Solid-fill mode (filled & background tiles held on target rails)
        self.solid_mode = False
        self.color_phase = _COLOR_BLACK
        self.tile_idx = 0

        self._bw_cmd = None
        self._rd_cmd = None
        self._bw_m = None
        self._bw_s = None
        self._rd_m = None
        self._rd_s = None
        self._lut_4rail = None
        self._lut_clear = None
        self._session_open = False
        self._exit_requested = False

        # Edge-detection state so short button taps are never missed during 580 ms panel refreshes
        self._prev_a = False
        self._prev_b = False
        self._prev_boot = False
        self._pending_toggle = False
        self._pending_restart = False

    def _ensure_buffers(self) -> None:
        if "SoftSPI" in repr(display.spi):
            try:
                display.spi = SPI(
                    1,
                    baudrate=20_000_000,
                    sck=EPD_SCL_PIN,
                    mosi=EPD_SDA_PIN,
                    miso=EPD_SPI_MISO_PIN,
                )
            except Exception:
                pass
        if self._bw_cmd is None:
            fb_len = len(display._framebuffer_black)
            self._bw_cmd = bytearray(fb_len)
            self._rd_cmd = bytearray(fb_len)
            self._bw_m = bytearray(_HALF_PLANE)
            self._bw_s = bytearray(_HALF_PLANE)
            self._rd_m = bytearray(_HALF_PLANE)
            self._rd_s = bytearray(_HALF_PLANE)
            self._lut_4rail = _make_4rail_lut(_STEP_FRAMES)
            self._lut_clear = _make_clear_lut()

    def _sync_leds(self) -> None:
        pos = (self.tile_idx * NUM_LEDS) // _TOTAL_TILES
        if self.color_phase == _COLOR_BLACK:
            fg = (0, 2, 4)    # Cyan for Black phase
        elif self.color_phase == _COLOR_RED:
            fg = (6, 0, 0)    # Red for Red phase
        else:
            fg = (3, 3, 3)    # White for White phase
        bg = (1, 1, 0) if self.solid_mode else (0, 0, 0)
        for i in range(NUM_LEDS):
            leds[i] = fg if i == pos else bg
        leds.write()

    def _poll_inputs(self) -> None:
        a_now = bool(UserA.value)
        b_now = bool(UserB.value)
        boot_now = bool(Boot.value)

        if UserA.pressed() or (a_now and not self._prev_a):
            self._pending_toggle = True
        if UserB.pressed() or (b_now and not self._prev_b) or Boot.pressed() or (boot_now and not self._prev_boot):
            self._pending_restart = True
        if MenuMinus.value or MenuPlus.value or MenuMinus._pressed_state or MenuPlus._pressed_state:
            self._exit_requested = True

        self._prev_a = a_now
        self._prev_b = b_now
        self._prev_boot = boot_now

    async def _wait_busy(self, target_idle: bool, timeout_ms: int = 10_000) -> bool:
        t0 = time.ticks_ms()
        while True:
            self._poll_inputs()
            idle = (EPD_BUSY1_PIN.value() == 1) and (EPD_BUSY2_PIN.value() == 1)
            if idle == target_idle:
                return True
            if time.ticks_diff(time.ticks_ms(), t0) >= timeout_ms:
                return False
            await aio.sleep_ms(4)

    def _write_planes_to_cogs(self) -> None:
        do_rev = 1 if display._reverse_fb_bits else 0
        _split_and_reverse(self._bw_cmd, self._bw_m, self._bw_s, _REVERSE_LUT, do_rev)
        _split_and_reverse(self._rd_cmd, self._rd_m, self._rd_s, _REVERSE_LUT, do_rev)

        display._send_index_data(0x01, b"\x00")  # DCTL
        display._send_index_data(0x13, b"\x00\x3b\x00\x00\x9f\x02")  # DUW
        display._send_index_data(0x90, b"\x00\x3b\x00\xa9")          # DRFW
        ram_rw = b"\x3b\x00\x14"
        display._send_index_data_left(0x12, ram_rw)
        display._send_index_data_left(0x10, self._bw_m)
        display._send_index_data_left(0x12, ram_rw)
        display._send_index_data_left(0x11, self._rd_m)
        display._send_index_data_right(0x12, ram_rw)
        display._send_index_data_right(0x10, self._bw_s)
        display._send_index_data_right(0x12, ram_rw)
        display._send_index_data_right(0x11, self._rd_s)

    async def _retrigger(self) -> None:
        display._send_index_data(0x15, b"\x3c")
        await self._wait_busy(False, 2000)
        await self._wait_busy(True, 15_000)

    async def _start_session(self, lut: bytes) -> None:
        """Reset COGs, load OTP trim registers (waiting 125 ms for OTP DMA), latch custom LUT, and soft-start HV."""
        await aio.sleep_ms(10)
        EPD_RST_PIN.value(1)
        await aio.sleep_ms(5)
        EPD_RST_PIN.value(0)
        await aio.sleep_ms(10)
        EPD_RST_PIN.value(1)
        await aio.sleep_ms(20)
        EPD_CS1_PIN.value(1)
        EPD_CS2_PIN.value(1)
        display._send_index_data(0x70, b"\x71\x36")  # Driver-B Errata 9 booster config

        self._write_planes_to_cogs()

        steps = (
            (0x05, b"\x7d", 50),
            (0x05, b"\x00", 10),
            (0xD8, b"\x00", 0),   # MS_SYNC
            (0xD6, b"\x00", 0),   # BVSS
            (0xA7, b"\x10", 25),
            (0xA7, b"\x00", 25),
            (0x03, b"\x00\x11", 0),
            (0x44, b"\x00", 0),
            (0x45, b"\x80", 0),
            (0xA7, b"\x10", 25),
            (0xA7, b"\x00", 25),
            (0x44, b"\x06", 0),
            (0x45, b"\x84", 0),   # Driver-B fast waveform bank
            (0xA7, b"\x10", 25),
            (0xA7, b"\x00", 125), # Wait 125 ms for internal OTP-to-SRAM DMA to finish before writing 0xB0
            (0x60, b"\x25", 0),   # TCON
            (0x02, b"\x25", 0),   # VCOM
        )
        for cmd, arg, wait_ms in steps:
            display._send_index_data(cmd, arg)
            if wait_ms:
                await aio.sleep_ms(wait_ms)
            self._poll_inputs()

        display._send_index_data_left(0x61, b"\x01")  # STV_DIR master only
        display._send_index_data(0xB0, lut)
        await display._dcdc_soft_start()
        self._session_open = True
        await self._wait_busy(True, 5000)

    async def _stop_session(self) -> None:
        if not self._session_open:
            return
        try:
            await self._wait_busy(True, 5000)
            display._send_index_data(0x09, b"\x7f")
            display._send_index_data(0x05, b"\x7d")
            display._send_index_data(0x09, b"\x00")
            await aio.sleep_ms(200)
            await self._wait_busy(True, 5000)
            EPD_DC_PIN.value(0)
            EPD_CS1_PIN.value(0)
            EPD_CS2_PIN.value(0)
            EPD_RST_PIN.value(0)
            EPD_CS1_PIN.value(1)
            EPD_CS2_PIN.value(1)
        finally:
            self._session_open = False

    def _phase_to_table(self, phase: int) -> int:
        if phase == _COLOR_BLACK:
            return 1  # VDH (+15 V Black)
        if phase == _COLOR_RED:
            return 3  # VDHR (+7 V Red)
        return 2      # VDL (-15 V White)

    def _prev_phase_to_table(self, phase: int) -> int:
        if phase == _COLOR_BLACK:
            return 2  # Before Black pass, screen is White (VDL)
        if phase == _COLOR_RED:
            return 1  # Before Red pass, screen is Black (VDH)
        return 3      # Before White pass, screen is Red (VDHR)

    async def _clear_canvas_white(self) -> None:
        """Fast 3-pass white clear using lutb_base, then latch the 4-rail LUT for animation."""
        fb_len = len(self._bw_cmd)
        _fill_buf(self._bw_cmd, fb_len, 0x00)
        _fill_buf(self._rd_cmd, fb_len, 0x00)
        if not self._session_open:
            await self._start_session(self._lut_clear)
        else:
            self._write_planes_to_cogs()
            display._send_index_data(0xB0, self._lut_clear)
        for _ in range(3):
            if self._exit_requested:
                return
            await self._retrigger()
        display._send_index_data(0xB0, self._lut_4rail)

    async def _launch_draw(self) -> None:
        self._ensure_buffers()
        self._exit_requested = False
        self._pending_toggle = False
        self._pending_restart = False
        self._prev_a = bool(UserA.value)
        self._prev_b = bool(UserB.value)
        self._prev_boot = bool(Boot.value)

        self.color_phase = _COLOR_BLACK
        self.tile_idx = 0
        self._sync_leds()
        await self._clear_canvas_white()

    async def _step_spiral(self) -> None:
        fb_len = len(self._bw_cmd)
        end_idx = self.tile_idx + _TILES_PER_STEP
        if end_idx > _TOTAL_TILES:
            end_idx = _TOTAL_TILES

        target_table = self._phase_to_table(self.color_phase)

        if self.solid_mode:
            # Solid mode: hold untouched background tiles on the previous pass's rail
            # and hold all tiles behind the wavefront on the current pass's rail so
            # gate-line scanning never bleeds charge off static Black or Red regions.
            bg_table = self._prev_phase_to_table(self.color_phase)
            _fill_buf(self._bw_cmd, fb_len, 0xFF if (bg_table & 1) else 0x00)
            _fill_buf(self._rd_cmd, fb_len, 0xFF if (bg_table & 2) else 0x00)
            start_paint = 0
        else:
            # Fading-tail snake mode: untouched tiles coast at 0 V (GND)
            _fill_buf(self._bw_cmd, fb_len, 0x00)
            _fill_buf(self._rd_cmd, fb_len, 0x00)
            start_paint = self.tile_idx

        if self.color_phase == _COLOR_RED and not self.solid_mode:
            # In snake mode on the Red pass, clear a 2-step (8-tile) white lookahead
            # window ahead of the Red head so Red develops saturated pigment cleanly.
            lead_end = end_idx + (_TILES_PER_STEP * 2)
            if lead_end > _TOTAL_TILES:
                lead_end = _TOTAL_TILES
            for i in range(end_idx, lead_end):
                col, row = _SPIRAL_PATH[i]
                _paint_tile(self._bw_cmd, self._rd_cmd, (col << 8) | row, 2)
        elif self.color_phase == _COLOR_RED and self.solid_mode:
            # In solid mode on the Red pass, bleach only the active 4-tile wavefront
            # to White for one step while previous tiles lock to Red (VDHR) and
            # untouched tiles stay pinned to Black (VDH).
            lead_end = end_idx + _TILES_PER_STEP
            if lead_end > _TOTAL_TILES:
                lead_end = _TOTAL_TILES
            for i in range(end_idx, lead_end):
                col, row = _SPIRAL_PATH[i]
                _paint_tile(self._bw_cmd, self._rd_cmd, (col << 8) | row, 2)

        for i in range(start_paint, end_idx):
            col, row = _SPIRAL_PATH[i]
            _paint_tile(self._bw_cmd, self._rd_cmd, (col << 8) | row, target_table)

        self._sync_leds()
        if not self._session_open:
            await self._start_session(self._lut_4rail)
        else:
            self._write_planes_to_cogs()
            display._send_index_data(0xB0, self._lut_4rail)
        await self._retrigger()

        self.tile_idx = end_idx
        if self.tile_idx >= _TOTAL_TILES:
            self.tile_idx = 0
            self.color_phase = (self.color_phase + 1) % 3

    async def arun(self) -> None:
        if self.launched:
            await self._launch_draw()
            if self._exit_requested:
                await self._stop_session()
            return

        self._poll_inputs()
        if self._exit_requested:
            await self._stop_session()
            return

        if self._pending_toggle:
            self._pending_toggle = False
            self.solid_mode = not self.solid_mode
            self._sync_leds()

        if self._pending_restart:
            self._pending_restart = False
            self.color_phase = _COLOR_BLACK
            self.tile_idx = 0
            self._sync_leds()
            await self._clear_canvas_white()
            if self._exit_requested:
                await self._stop_session()
            return

        try:
            await self._step_spiral()
        finally:
            if self._exit_requested:
                await self._stop_session()
        await aio.sleep_ms(self.sleep_ms)
