"""Biomorph partial-refresh animation for the 2026 Supercon Badge.

Evaluates a 240x168 complex dynamical system with Pickover orbit traps on each step and expands
each cell to 960x672 using 4x4 Bayer stipple patterns over a 4-rail LUT.

Credits & References:
  - Clifford A. Pickover, "Biomorphs: Computer Displays of Biological Forms Generated from
    Mathematical Feedback Loops" (Computer Graphics Forum, 1986): Orbit-trap stalk method.
  - TheESTest/Supercon_2026_Badge_Firmware (badge_files/lib/epd_fast.py): MicroPython fast-refresh
    driver (FastRefresh, make_lut, OTP reader, and persistent HV session management).
  - phthallo/supercon (firmware/lib/epd.py, firmware/lib/luts.py): 20-frame register 0xB0 custom
    LUT structure (off = 0, 2-frame minimum group timing) and delta refresh technique.
  - Hack-a-Day/2026-Billboard_Badge (firmware/esp32s3-itc97/src/epd97.cpp, commit 6ba6dd8) &
    PervasiveDisplays/EPD_Driver_GU_large: Official C++ fast-mode driver and lutb_base.
  - @trandi (PR #4) & @FacioErgoSum (PR #3): 20 MHz hardware SPI(1) and @micropython.viper plane helpers.

Controls:
  User A: Toggle between drift mode (default, outer bands at GND) and crisp mode (outer bands at VDL White)
  User B: Cycle dynamical system (0 = Pickover Biomorph Julia, 1 = Burning Ship, 2 = Folded Vortex)
  Boot:   Advance orbit phase by 17 steps
"""

import asyncio as aio
import binascii
import time

import micropython
from machine import SPI, Pin
from micropython import const

from lib import epd as _epd

OTP_LEN = const(88)
REG45_B1 = const(0x84)  # waveform bank on the second 0x45 write, driver B
PULLS = const(3)  # refresh passes per frame with the official table, driver B
SESSION_IMAGES = const(20)  # the C++ driver's session limit (max_images default)
SESSION_IDLE_MS = const(10_000)  # close an open session after this long idle
REFRESH_TIMEOUT_MS = const(20_000)  # one fast pass takes at most about 1.2 s
HW_SPI_HZ = const(20_000_000)  # plane clock; Pervasive Displays rate writes at 20 MHz

GND = const(0)
VDH = const(1)  # full positive: towards black
VDL = const(2)  # negative: towards white
VDHR = const(3)  # reduced positive: red

_ROW = const(120)  # framebuffer bytes per line
_HALF = const(60)  # bytes per line for each of the two COGs
_HALF_PLANE = const(40320)  # 60 bytes x 672 lines

_DC = _epd.EPD_DC_PIN
_RST = _epd.EPD_RST_PIN
_CS_M = _epd.EPD_CS1_PIN  # master COG, left half
_CS_S = _epd.EPD_CS2_PIN  # slave COG, right half
_BUSY_M = _epd.EPD_BUSY1_PIN
_BUSY_S = _epd.EPD_BUSY2_PIN
_SCK = _epd.EPD_SCL_PIN
_SDA = _epd.EPD_SDA_PIN
_MISO = _epd.EPD_SPI_MISO_PIN  # not connected; claimed so SPI leaves real pins alone
_display = _epd.display
_spi = _display.spi

_HOLD = b"\xff" * _HALF_PLANE  # table 3 everywhere: the official table drives none of it
_ZERO = bytes(_HALF_PLANE)  # table 0 everywhere
_IDX = [bytes((i,)) for i in range(256)]  # register indices, so a write allocates nothing


# --- waveform tables --------------------------------------------------------------
def _frames_per_group(frames):
    """Split a frame count over the 10 groups, at least 2 frames (1 + 1) each."""
    frames = max(20, frames)
    split = []
    for g in range(10):
        n = frames // 10 + (1 if g < frames % 10 else 0)
        on1 = (n + 1) // 2
        split.append((on1, n - on1))
    return split


def make_lut(levels, frames):
    """A 400-byte driver-B table.

    levels(table, group) returns (first_half, second_half) rails for tables 0-3
    (table 4, VCOM, stays at ground). Every table gets the same group timing,
    since a group ends when its shortest table does. Off counts are 0, as in
    phthallo/supercon's delta table; on, ph2 and repeat stay at least 1 (a zero
    there makes a group run for seconds).
    """
    out = bytearray(400)
    for g, (on1, on2) in enumerate(_frames_per_group(frames)):
        for t in range(5):
            a, b = levels(t, g) if t < 4 else (GND, GND)
            q = (t * 10 + g) * 8
            out[q:q + 8] = bytes(((a << 6) | (a << 4) | (b << 2) | b, on1, 0, 1, on2, 0, 1, 1))
    return bytes(out)


def delta_lut(frames, white_push=0, black_push=0, hold_push=0, hold_rail=VDHR, black_frames=None):
    """Change-only table: table (old << 1) | new; the two changing tables get full drive.

    black_frames caps how many frames at the end of the pass Table 1 (white-to-black)
    drives VDH (default: all frames), so frames can be raised (e.g. 32) to give
    Table 2 (black-to-white) enough VDL time to fully clear without over-driving black.

    Pixels that keep their colour sit at ground, which is not neutral over many
    passes: black relaxes to grey and white drifts. white_push / black_push give
    them that many frames of push towards their own colour at the end of every
    pass (phthallo/supercon uses 6 and 2 of 20): stays-white gets VDL,
    stays-black VDH. Only use pushes where everything the controller scans is
    black or white; red would be pushed too.

    hold_push instead gives table 3 that many frames of hold_rail (default VDHR,
    the reduced positive rail, which holds red up without bringing black up).
    Use it when table 3 carries only red "hold" pixels: send pixels that stay
    black as white-to-black (old bit clear) so they leave table 3.
    """
    split = _frames_per_group(frames)
    total = sum(a + b for a, b in split)
    halves = []  # first frame of each group's two halves
    f = 0
    for on1, on2 in split:
        halves.append((f, f + on1))
        f += on1 + on2

    def levels(t, g):
        if t == 2:
            return VDL, VDL
        if t == 1:
            push, rail = (total if black_frames is None else black_frames), VDH
        elif t == 0:
            push, rail = white_push, VDL
        elif t == 3:
            push, rail = (hold_push, hold_rail) if hold_push else (black_push, VDH)
        else:
            push, rail = 0, GND
        h1, h2 = halves[g]
        return (rail if h1 >= total - push else GND), (rail if h2 >= total - push else GND)

    return make_lut(levels, frames)


def ground_lut(frames):
    """Every table at ground: drives nothing. For timing the panel without drawing."""
    return make_lut(lambda t, g: (GND, GND), frames)


# --- bus ----------------------------------------------------------------------
def _select(m, s):
    # Raise the side that is not wanted before lowering the one that is, so the
    # two COGs never both listen by accident.
    if not m:
        _CS_M.value(1)
    if not s:
        _CS_S.value(1)
    if m:
        _CS_M.value(0)
    if s:
        _CS_S.value(0)


def _deselect():
    _CS_M.value(1)
    _CS_S.value(1)


def _send(index, data=b"", m=True, s=True):
    """Register write, to both COGs unless told otherwise."""
    _DC.value(0)
    _select(m, s)
    _spi.write(_IDX[index])
    _deselect()
    if data:
        _DC.value(1)
        _select(m, s)
        _spi.write(data)
        _deselect()


def _idle():
    return _BUSY_M.value() == 1 and _BUSY_S.value() == 1


async def _wait(idle, timeout_ms):
    """Wait for both BUSY lines to be high (idle=True) or either low. ms, or -1."""
    t0 = time.ticks_ms()
    while _idle() != idle:
        if time.ticks_diff(time.ticks_ms(), t0) >= timeout_ms:
            return -1
        await aio.sleep_ms(2)
    return time.ticks_diff(time.ticks_ms(), t0)


# The OTP is clocked at a few tens of kHz with the data pin turned around; the
# panel does not follow a full-speed bitbang for it.
def _bb_out_slow(v):
    for _ in range(8):
        _SDA.value(v & 0x80)
        time.sleep_us(10)
        _SCK.value(1)
        time.sleep_us(10)
        _SCK.value(0)
        time.sleep_us(10)
        v <<= 1


def _bb_in_slow():
    v = 0
    for _ in range(8):
        _SCK.value(1)
        time.sleep_us(10)
        v = (v << 1) | _SDA.value()
        _SCK.value(0)
        time.sleep_us(10)
    return v


def _stage_delay(d):
    if d & 0x80:
        time.sleep_ms((d & 0x7F) or 1)
    else:
        time.sleep_us(10 * (d & 0x7F))


@micropython.viper
def _fill_window(bw, rd, black, red, y0: int, y1: int, lo: int, n: int, half_off: int):
    """Hold mode: lines y0-y1, byte columns lo..lo+n-1, into one COG's half
    planes, red drawn as ink and the red plane cleared."""
    db = ptr8(bw)
    dr = ptr8(rd)
    sb = ptr8(black)
    sr = ptr8(red)
    y = y0
    while y <= y1:
        s = y * 120 + lo
        d = y * 60 + lo - half_off
        i = 0
        while i < n:
            db[d + i] = sb[s + i] | sr[s + i]
            dr[d + i] = 0
            i += 1
        y += 1


@micropython.viper
def _pack_rows(dst, src, hold, y0: int, y1: int, lo: int, n: int):
    """Lines y0-y1, byte columns lo..lo+n-1 of a framebuffer OR the hold mask,
    packed row by row."""
    d = ptr8(dst)
    s = ptr8(src)
    h = ptr8(hold)
    k = 0
    y = y0
    while y <= y1:
        b = y * 120 + lo
        i = 0
        while i < n:
            d[k] = s[b + i] | h[b + i]
            k += 1
            i += 1
        y += 1


@micropython.viper
def _fill_delta(bw, rd, new, old, hold, y0: int, y1: int, lo: int, n: int):
    """Change-only mode: the new frame into the bw plane, the old one into the red
    plane, and hold-mask pixels into both (table 3). lo >= 60 is the slave COG."""
    db = ptr8(bw)
    dr = ptr8(rd)
    sn = ptr8(new)
    so = ptr8(old)
    sh = ptr8(hold)
    half_off = 0
    if lo >= 60:
        half_off = 60
    y = y0
    while y <= y1:
        s = y * 120 + lo
        d = y * 60 + lo - half_off
        i = 0
        while i < n:
            db[d + i] = sn[s + i] | sh[s + i]
            dr[d + i] = so[s + i] | sh[s + i]
            i += 1
        y += 1


# --- OTP ----------------------------------------------------------------------
def _looks_like_driver_b(raw):
    return raw[0x16] == 0x3B and raw[0x0D] == 0x3B and (raw[0x28] & 0x80) != 0


def _remap_b_to_8(o):
    """Driver B's block, moved into the driver-8 layout the rest reads."""
    for i in range(0x1E, 0x12, -1):
        o[i] = o[i - 1]
    for i in range(32):
        o[0x20 + i] = o[0x28 + i]
    o[0] = 0xA5


def _otp_problem(o):
    """None if the block is usable, else what is wrong with it."""
    # A ramp stage of 0xFF ramps the booster 127 times and hangs BUSY, so all
    # four are checked before anything uses them.
    for stage in range(4):
        sb = o[0x20 + 8 * stage]
        rp = sb & 0x7F
        if sb == 0xFF or rp == 0 or rp > 40:
            return "DC/DC ramp stage %d implausible: 0x%02X" % (stage, sb)
    if o[0x17] != 0x3B or o[0x0D] != 0x3B:
        return "window check failed: DUW xe=0x%02X DRFW xe=0x%02X" % (o[0x17], o[0x0D])
    return None


def _hex(b):
    return binascii.hexlify(b, " ").decode()


class FastRefresh:
    def __init__(self):
        self.otp = None  # 88-byte calibration block, driver-8 layout, once read
        self.raw = None  # the block as read, for diagnostics
        self.problem = None
        self.session_open = False
        self.latched = False
        self.session_images = 0
        self.last_ms = 0
        self.verbose = False
        """Print each step of the bring-up (with a pause so USB gets it out)."""
        self.log = True
        """Print the timings of every refresh."""
        self.hw_spi = True
        """Send the planes over the hardware SPI bus. False uses lib/epd.py's bit-banged bus."""
        self.spi_hz = HW_SPI_HZ
        """Hardware SPI clock for the planes."""
        self.quick = True
        """Start a session with Pervasive Displays' waits for this COG family
        (reset 5/2/4/20 ms, load strobes 2/10 ms) instead of the official 1.2 s
        register sequence and 0.66 s settle. False uses the official timings."""
        self.osc = None
        """If set, write 0x03 = (0x00, osc) after the first load strobe, as the stock
        driver does (0x11). The official driver never sends it on driver B."""
        self.second_pass_watch = False
        """Watch 1 s after a session's first pass for a second waveform pass (driver 8 does that)."""
        self.max_images = SESSION_IMAGES
        """Frames per session before the controller is shut down and started again."""
        self.window_writes = False
        """Change-only mode with scan_rows: write only the scanned lines, full width,
        into the controller's RAM (DUW 0x13 and RAM_RW 0x12 narrowed in y) instead
        of both full planes. Needs the scanned lines to start above line 256."""
        self._ram_ready = False  # this session's RAM holds a full change-only frame
        self._no_hold = None  # an all-zero hold mask, made on first use
        self._dcdc_on = False
        self._pass_ms = []
        self.last = {}
        """Timings and settings of the last refresh, for tuning."""
        self._lut = None  # the official table, loaded on first use
        self._latched_lut = None  # the table the open session latched
        self._lock = aio.Lock()
        self._guard_task = None
        self._bufs = [bytearray(_HALF_PLANE) for _ in range(4)]  # bw M/S, red M/S

    # --- public ---------------------------------------------------------------
    async def probe(self):
        """Read the panel's OTP once. True if it is a usable driver-B panel."""
        if self.otp is not None:
            return True
        await self._acquire()
        try:
            return await self._probe()
        finally:
            self._lock.release()

    async def refresh(self, window, black=None, red=None, keep_awake=True, pulls=PULLS,
                      refresh_rows=None, old=None, lut=None, scan_rows=None, hold=None):
        """Fast-refresh the box window = (x0, y0, x1, y1), pixels inclusive.

        black/red are full-screen framebuffers (default: the display's own).
        With old=None ("hold" mode) pixels outside the window are not driven.
        With old= a full-screen framebuffer of what the panel shows now, the
        planes carry (old, new) for a change-only table such as delta_lut(n);
        pixels outside the window then go out as table 0.
        window=None drives nothing at all (for timing).
        pulls is the number of passes, refresh_rows=(first, last) narrows the
        controller's refresh window to those lines, lut is the table to latch
        (default: the official one).
        Change-only mode with window_writes and scan_rows=(first, last), first
        below 256: the controller scans only those lines (data window and
        refresh window both narrowed in y), and its RAM is written only there,
        across the full width. Lines outside are never scanned, so they are
        never driven. (Narrowing the data window in x as well does not work on
        this panel: the controller keeps writing full 60-byte rows, so a narrow
        window's data wraps across them and shows up as several squashed copies.)
        hold, change-only mode only: a full-screen framebuffer whose set pixels
        go out with both plane bits set, i.e. as table 3 (see delta_lut's
        hold_push). Pass the red framebuffer to keep red on the scanned lines
        out of the black and white tables.
        Returns True on success.
        """
        if black is None:
            black = _display._framebuffer_black
        if red is None:
            red = _display._framebuffer_red
        if hold is None:
            if self._no_hold is None:
                self._no_hold = bytes(len(black))
            hold = self._no_hold
        if window is not None:
            x0, y0, x1, y1 = window
            window = (max(0, min(x0, x1)), max(0, min(y0, y1)),
                      min(959, max(x0, x1)), min(671, max(y0, y1)))
        await self._acquire()
        try:
            if self.otp is None and not await self._probe():
                return False
            if lut is None:
                lut = self._lut
            if self.session_open and lut != self._latched_lut:
                await self._stop()  # a session latches one table only
            t0 = time.ticks_ms()
            region = None  # what the controller's RAM gets, for window writes
            if (old is not None and self.window_writes and window is not None
                    and scan_rows is not None and scan_rows[0] < 256):
                s0, s1 = scan_rows
                region = (0, s0, 959, s1)  # the full width of the scanned lines
                refresh_rows = scan_rows
            if region is None:
                if old is None:
                    self._build_hold(black, red, window)
                else:
                    self._build_delta(black, old, window, hold)
            t1 = time.ticks_ms()
            first = not self.session_open
            if first:
                await self._open()
            t2 = time.ticks_ms()
            if region is not None:
                self._write_window(black, old, region, refresh_rows, hold)
            else:
                self._write_planes(refresh_rows)
            self._ram_ready = old is not None
            windowed = region is not None
            t3 = time.ticks_ms()
            self._pass_ms = []
            rc = await (self._retrigger() if self.latched else self._first_refresh(lut))
            done = 1
            while rc == 0 and done < pulls:
                rc = await self._retrigger()
                done += 1
            t4 = time.ticks_ms()
            self.latched = rc == 0
            self.session_images += 1
            self.last_ms = time.ticks_ms()
            kept = rc == 0 and keep_awake and self.session_images < self.max_images
            if not kept:
                await self._stop()
            elif self._guard_task is None:
                self._guard_task = aio.create_task(self._guard())
            self.last = {
                "ok": rc == 0,
                "rc": rc,
                "mode": "hold" if old is None else "delta",
                "windowed": windowed,
                "window": window,
                "refresh_rows": refresh_rows,
                "pulls": done,
                "pass_ms": self._pass_ms,
                "new_session": first,
                "kept_awake": kept,
                "build_ms": time.ticks_diff(t1, t0),
                "open_ms": time.ticks_diff(t2, t1),
                "planes_ms": time.ticks_diff(t3, t2),
                "refresh_ms": time.ticks_diff(t4, t3),
                "total_ms": time.ticks_diff(time.ticks_ms(), t0),
            }
            if self.log:
                print("epd_fast:", self.last)
            return rc == 0
        except BaseException:
            # Whatever went wrong, do not leave the booster running.
            await self._stop()
            raise
        finally:
            self._lock.release()

    async def close(self):
        """Shut the controller down if a fast session is open."""
        if not self.session_open:
            return
        await self._acquire()
        try:
            if self.session_open:
                await self._stop()
        finally:
            self._lock.release()

    async def _close_for_stock(self):
        """close(), for a normal update that is already under way.

        That update is what keeps _stock_busy() true, so waiting for it the way
        close() does would wait forever. Only a fast refresh still in progress
        is waited for, and that never waits for a normal update while it holds
        the lock.
        """
        if not self.session_open:
            return
        await self._lock.acquire()
        try:
            if self.session_open:
                await self._stop()
        finally:
            self._lock.release()

    def info(self):
        """What the OTP says, next to what the stock driver (lib/epd.py) sends."""
        if self.otp is None:
            return {"panel": "unknown", "problem": self.problem,
                    "raw": _hex(self.raw) if self.raw else None}
        o = self.otp
        return {
            "panel": "E2969JS0B1 (driver B)",
            "TCON 0x60": "0x%02X (stock sends 0x25)" % o[0x0B],
            "STV_DIR 0x61": "0x%02X (stock sends 0x01, master only)" % o[0x1C],
            "DCTL 0x01": "0x%02X (stock never sends it)" % o[0x10],
            "VCOM 0x02": "0x%02X (stock sends 0x00)" % o[0x11],
            "MS_SYNC 0xD8": "0x%02X (stock sends 0x80)" % o[0x1D],
            "BVSS 0xD6": "0x%02X (stock sends 0x00)" % o[0x1E],
            "DRFW 0x90": "%s (stock: 00 3b 00 a9)" % _hex(o[0x0C:0x10]),
            "DUW 0x13": "%s (stock: 00 3b 00 00 9f 02)" % _hex(o[0x16:0x1C]),
            "RAM_RW 0x12": "%s (stock: 3b 00 14)" % _hex(o[0x13:0x16]),
            "DC/DC ramp": [_hex(o[0x20 + 8 * i:0x28 + 8 * i]) for i in range(4)],
            "raw": _hex(self.raw),
        }

    # --- internals ------------------------------------------------------------
    def _log(self, msg):
        if self.verbose:
            print("epd_fast:", msg)
            time.sleep_ms(50)

    def _stock_busy(self):
        return _display._framebuffer_ready.is_set() or not _display.is_done_updating()

    async def _acquire(self):
        # Never hold the lock while waiting for a normal update: that update
        # closes the fast session first, which needs the lock.
        while True:
            while self._stock_busy():
                await aio.sleep_ms(100)
            await self._lock.acquire()
            if not self._stock_busy():
                return
            self._lock.release()

    async def _pulse_reset(self, wait_ms, hi_ms, lo_ms, set_ms):
        await aio.sleep_ms(wait_ms)
        _RST.value(1)
        await aio.sleep_ms(hi_ms)
        _RST.value(0)
        await aio.sleep_ms(lo_ms)
        _RST.value(1)
        await aio.sleep_ms(set_ms)

    async def _probe(self):
        # Use short reset waits when self.quick is enabled (the panel supply is always on).
        self._log("probe: COG reset")
        if self.quick:
            await self._pulse_reset(5, 2, 4, 20)
        else:
            await self._pulse_reset(800, 50, 100, 400)
        # Driver B opens its OTP with 0x70 to both COGs, then 0xB9 to the
        # master, whose answer is read back on the data line.
        self._log("probe: 0x70 to both COGs")
        _send(0x70, b"\x71\x36")
        self._log("probe: 0xB9 opener, reading %d bytes" % OTP_LEN)
        _DC.value(0)
        _select(True, False)
        _bb_out_slow(0xB9)
        time.sleep_ms(1)
        _DC.value(1)
        _SDA.init(Pin.IN, Pin.PULL_DOWN)
        try:
            _bb_in_slow()  # dummy byte
            raw = bytearray(OTP_LEN)
            for i in range(OTP_LEN):
                raw[i] = _bb_in_slow()
        finally:
            _deselect()
            _SDA.init(Pin.OUT, value=0)
        self.raw = bytes(raw)
        self._log("probe: read " + _hex(self.raw[:16]) + " ...")
        o = bytearray(raw)
        if not _looks_like_driver_b(o):
            self.problem = "OTP does not look like driver B (E2969JS0B1)"
        else:
            _remap_b_to_8(o)
            self.problem = _otp_problem(o)
        self._log("probe: second reset, then park the COG")
        if self.quick:
            await self._pulse_reset(2, 0, 4, 10)
            _DC.value(1)
            _RST.value(1)
            _deselect()
        else:
            await self._pulse_reset(50, 0, 100, 400)
            await self._stop()
        self._log("probe: done")
        if self.problem:
            print("epd_fast:", self.problem, "- raw:", _hex(self.raw))
            return False
        self.otp = bytes(o)
        if self._lut is None:
            from data import rendered_luts

            lut = bytes(rendered_luts.lutb_base)
            if len(lut) != 400:
                raise ValueError("lutb_base is %d bytes, expected 400" % len(lut))
            self._lut = lut
        return True

    def _build_hold(self, black, red, window):
        bw_m, bw_s, rd_m, rd_s = self._bufs
        for b in self._bufs:
            b[:] = _HOLD
        if window is None:
            return
        x0, y0, x1, y1 = window
        c0, c1 = x0 >> 3, x1 >> 3
        for half in (0, 1):
            lo = max(c0, half * _HALF)
            hi = min(c1, half * _HALF + _HALF - 1)
            if lo > hi:
                continue
            # Red cannot be driven fast, so it goes out as ink.
            if half == 0:
                _fill_window(bw_m, rd_m, black, red, y0, y1, lo, hi - lo + 1, 0)
            else:
                _fill_window(bw_s, rd_s, black, red, y0, y1, lo, hi - lo + 1, _HALF)

    def _build_delta(self, new, old, window, hold):
        bw_m, bw_s, rd_m, rd_s = self._bufs
        for b in self._bufs:
            b[:] = _ZERO
        if window is None:
            return
        x0, y0, x1, y1 = window
        c0, c1 = x0 >> 3, x1 >> 3
        for half in (0, 1):
            lo = max(c0, half * _HALF)
            hi = min(c1, half * _HALF + _HALF - 1)
            if lo > hi:
                continue
            if half == 0:
                _fill_delta(bw_m, rd_m, new, old, hold, y0, y1, lo, hi - lo + 1)
            else:
                _fill_delta(bw_s, rd_s, new, old, hold, y0, y1, lo, hi - lo + 1)

    async def _open(self):
        # The C++ driver settles 800 ms here because it has just switched the
        # panel supply on; this badge never switches it off.
        if self.quick:
            await self._pulse_reset(5, 2, 4, 20)
        else:
            await self._pulse_reset(200, 50, 100, 400)
        self.session_open = True
        self.latched = False
        self.session_images = 0
        self._ram_ready = False

    def _bus(self):
        """The hardware SPI bus, borrowing SCK and SDA from the bit-banged one for
        bulk data only (MISO goes to the unconnected pin), or None."""
        if self.hw_spi:
            try:
                return SPI(1, baudrate=self.spi_hz, polarity=0, phase=0,
                           sck=_SCK, mosi=_SDA, miso=_MISO)
            except Exception as ex:
                print("epd_fast: hardware SPI unavailable (%s), using the bit-banged bus" % ex)
                self.hw_spi = False
        return None

    def _release(self, bus):
        if bus is not None:
            bus.deinit()
            # Hand the pins back to the bit-banged bus as plain outputs.
            _SCK.init(Pin.OUT, value=0)
            _SDA.init(Pin.OUT, value=0)

    def _plane(self, index, m, s):
        """A full plane: the index to both COGs, then each COG's half."""
        _DC.value(0)
        _select(True, True)
        _spi.write(_IDX[index])
        _deselect()
        _DC.value(1)
        bus = self._bus()
        try:
            out = bus if bus is not None else _spi
            _select(True, False)
            out.write(m)
            _deselect()
            _select(False, True)
            out.write(s)
            _deselect()
        finally:
            self._release(bus)

    def _plane_one(self, index, data, m, s):
        """Plane data for one COG (m or s) only."""
        _DC.value(0)
        _select(m, s)
        _spi.write(_IDX[index])
        _deselect()
        _DC.value(1)
        bus = self._bus()
        try:
            _select(m, s)
            (bus if bus is not None else _spi).write(data)
            _deselect()
        finally:
            self._release(bus)

    def _write_window(self, new, old, window, rows, hold):
        """Change-only planes for the window only: DUW and RAM_RW narrowed per COG.

        DUW is (x start, x end, y start lo/hi, y end lo/hi) in bytes and lines,
        RAM_RW is (x start, y start lo, the OTP's third byte); both decoded from
        Pervasive Displays' values for the other panel sizes.
        """
        o = self.otp
        x0, y0, x1, y1 = window
        c0, c1 = x0 >> 3, x1 >> 3
        lines = y1 - y0 + 1
        _send(0x01, o[0x10:0x11])  # DCTL
        self._send_drfw(rows)  # DRFW
        for half in (0, 1):
            lo = max(c0, half * _HALF)
            hi = min(c1, half * _HALF + _HALF - 1)
            if lo > hi:
                continue
            m, s = half == 0, half == 1
            xa, xb = lo - half * _HALF, hi - half * _HALF
            n = hi - lo + 1
            bw = memoryview(self._bufs[0])[:n * lines]
            rd = memoryview(self._bufs[2])[:n * lines]
            _pack_rows(bw, new, hold, y0, y1, lo, n)
            _pack_rows(rd, old, hold, y0, y1, lo, n)
            ram_rw = bytes((xa, y0 & 0xFF, o[0x15]))
            _send(0x13, bytes((xa, xb, y0 & 0xFF, y0 >> 8, y1 & 0xFF, y1 >> 8)), m, s)  # DUW
            _send(0x12, ram_rw, m, s)
            self._plane_one(0x10, bw, m, s)
            _send(0x12, ram_rw, m, s)
            self._plane_one(0x11, rd, m, s)

    def _drfw(self, rows):
        """The refresh window: the OTP's, or narrowed to lines rows=(first, last)."""
        d = bytearray(self.otp[0x0C:0x10])  # x start, x end, line start/4, line end/4
        if rows is not None:
            first, last = rows
            d[2] = max(0, min(first, last)) >> 2
            d[3] = min(d[3], (max(first, last) >> 2) + 1)
        return d

    def _send_drfw(self, rows):
        """DRFW to both COGs, full width. (Narrowing its x range per COG broke the
        picture on this panel, so only the lines are narrowed.)"""
        _send(0x90, self._drfw(rows))

    def _write_planes(self, rows=None):
        o = self.otp
        bw_m, bw_s, rd_m, rd_s = self._bufs
        _send(0x01, o[0x10:0x11])  # DCTL: driver B sets it before the windows
        _send(0x13, o[0x16:0x1C])  # DUW
        self._send_drfw(rows)  # DRFW
        _send(0x12, o[0x13:0x16])  # RAM_RW
        self._plane(0x10, bw_m, bw_s)
        _send(0x12, o[0x13:0x16])
        self._plane(0x11, rd_m, rd_s)

    def _soft_start(self):
        """The vendor's DC/DC ramp, with the panel's own OTP values."""
        o = self.otp
        for stage in range(4):
            off = 0x20 + 8 * stage
            rep = o[off] & 0x7F
            if o[off] & 0x80:
                ph0 = o[off + 1]
                ph1 = o[off + 2]
                for _ in range(rep):
                    _send(0x09, o[off + 5:off + 6])
                    ph0 = (ph0 + o[off + 3]) & 0xFF
                    ph1 = (ph1 + o[off + 4]) & 0xFF
                    _send(0x51, bytes((ph0, ph1)))
                    _send(0x09, o[off + 6:off + 7])
                    _stage_delay(o[off + 7])
            else:
                for _ in range(rep):
                    _send(0x09, o[off + 1:off + 2])
                    _stage_delay(o[off + 3])
                    _send(0x09, o[off + 2:off + 3])
                    _stage_delay(o[off + 4])

    async def _first_refresh(self, lut):
        """The full update of a session's first frame, which latches the waveform."""
        o = self.otp
        if self.quick:
            pre, w05a, w05b, hi, lo, last, post = 10, 50, 10, 25, 25, 125, 10
        else:
            pre, w05a, w05b, hi, lo, last, post = 660, 200, 10, 100, 100, 500, 100
        await aio.sleep_ms(pre)
        steps = [
            (0x05, b"\x7d", w05a),
            (0x05, b"\x00", w05b),
            (0xD8, o[0x1D:0x1E], 0),  # MS_SYNC
            (0xD6, o[0x1E:0x1F], 0),  # BVSS
            (0xA7, b"\x10", hi),
            (0xA7, b"\x00", lo),
        ]
        if self.osc is not None:
            steps.append((0x03, bytes((0x00, self.osc)), 0))
        steps += [
            (0x44, b"\x00", 0),
            (0x45, b"\x80", 0),
            (0xA7, b"\x10", hi),
            (0xA7, b"\x00", lo),
            (0x44, b"\x06", 0),
            (0x45, bytes((REG45_B1,)), 0),
            (0xA7, b"\x10", hi),
            (0xA7, b"\x00", last),
            (0x60, o[0x0B:0x0C], 0),  # TCON
            (0x02, o[0x11:0x12], 0),  # VCOM
        ]
        for cmd, arg, ms in steps:
            _send(cmd, arg)
            if ms:
                await aio.sleep_ms(ms)
        _send(0x61, o[0x1C:0x1D], m=True, s=False)  # STV_DIR: master COG only
        # After the OTP waveform load above, before the soft start, to both COGs.
        _send(0xB0, lut)
        self._latched_lut = lut
        self._dcdc_on = True
        self._soft_start()
        await aio.sleep_ms(post)
        if not _idle() and await _wait(True, 10_000) < 0:
            print("epd_fast: booster never reported ready, stopping")
            return -1
        rc = await self._retrigger()
        if rc == 0 and self.second_pass_watch:
            t0 = time.ticks_ms()
            while time.ticks_diff(time.ticks_ms(), t0) < 1000:
                await aio.sleep_ms(10)
                if not _idle():
                    if await _wait(True, REFRESH_TIMEOUT_MS) < 0:
                        return -3
                    break
        return rc

    async def _retrigger(self):
        """Run the refresh again on the waveform, planes and rails already loaded."""
        _send(0x15, b"\x3c")
        if await _wait(False, 2000) < 0:
            print("epd_fast: BUSY never went low after 0x15")
            return -2
        ms = await _wait(True, REFRESH_TIMEOUT_MS)
        if ms < 0:
            print("epd_fast: refresh timed out")
            return -3
        self._pass_ms.append(ms)
        return 0

    async def _stop(self):
        """The C++ driver's cog_stop(), minus cutting the panel supply.

        The DC/DC shutdown writes only go out if the booster was started: on an
        idle COG, 0x09 = 0x7F would switch its stages on without the ramp. The
        C++ driver gets away with it because it cuts the panel supply next.
        """
        if self._dcdc_on:
            _send(0x09, b"\x7f")
            await aio.sleep_ms(20)
            _send(0x05, b"\x7d")
            _send(0x09, b"\x00")
            await aio.sleep_ms(200)
            await _wait(True, 2000)
            self._dcdc_on = False
        _DC.value(1)
        _RST.value(1)
        _deselect()
        self.session_open = False
        self.latched = False
        self._latched_lut = None
        self._ram_ready = False

    async def _guard(self):
        try:
            while self.session_open:
                await aio.sleep_ms(500)
                if (self.session_open and not self._lock.locked()
                        and time.ticks_diff(time.ticks_ms(), self.last_ms) > SESSION_IDLE_MS):
                    await self.close()
        finally:
            self._guard_task = None


try:
    from lib.epd_fast import fast, make_lut  # type: ignore
except ImportError:
    fast = getattr(_epd, "_fast_refresh", None)
    if fast is None:
        fast = FastRefresh()
        _epd._fast_refresh = fast

    # A normal display.update() always closes an open fast session first, so the
    # two never share the bus.
    _cls = _epd.EPaperDisplay
    if not hasattr(_cls, "_stock_global_update"):
        _cls._stock_global_update = _cls.global_update

        async def _global_update(self, *planes):
            await fast._close_for_stock()
            await self._stock_global_update(*planes)

        _cls.global_update = _global_update


# --- Biomorph App -------------------------------------------------------------
import array
import asyncio as aio
import math
import micropython
from micropython import const

from apps.base_app import BaseApp
from lib.button import Boot, UserA, UserB
from lib.epd import display, display_height, display_width
from lib.led_bar import NUM_LEDS, leds

_STEP_FRAMES = const(18)

_GND = const(0)
_VDH = const(1)   # +15 V: Black
_VDL = const(2)   # -15 V: White
_VDHR = const(3)  # +7 V:  Red

_FULL_WINDOW = (0, 0, display_width - 1, display_height - 1)

# 4x4 Bayer halftone nibble patterns for density levels 0..4 (4 rows per density level)
_DITHER_PATTERNS = bytes([
    0x0, 0x0, 0x0, 0x0,  # dens 0: 0%
    0x8, 0x0, 0x2, 0x0,  # dens 1: 25% stipple
    0xA, 0x5, 0xA, 0x5,  # dens 2: 50% checkerboard
    0xE, 0x5, 0xB, 0x7,  # dens 3: 75% stipple
    0xF, 0xF, 0xF, 0xF,  # dens 4: 100% solid
])


def _make_4rail_lut(frames: int) -> bytes:
    """400-byte LUT exposing all 4 physical rails per pixel via (rd_bit << 1) | bw_bit,
    with Group 9B grounded to 0 V so pixel capacitors discharge cleanly after each step.
    """
    def levels(t: int, g: int):
        if t == 1:
            return (_VDH, _GND) if g == 9 else (_VDH, _VDH)
        if t == 2:
            return (_VDL, _GND) if g == 9 else (_VDL, _VDL)
        if t == 3:
            return (_VDHR, _GND) if g == 9 else (_VDHR, _VDHR)
        return (_GND, _GND)

    return make_lut(levels, frames)


@micropython.viper
def _render_fractal_frame(bw: ptr8, rd: ptr8, params: ptr32, dither: ptr8):
    """Render a slice of the 240x168 orbit-trap fractal field with 4x4 Bayer stippling
    to 960x672 (80,640 bytes). Keeps local variable count <= 15 so Xtensa's 8-bit
    signed stack adjustment immediate (`addi a1, a1, imm8`) never overflows.

    params[0] = cx (Q12)
    params[1] = cy (Q12)
    params[2] = dx (Q12 step per grid cell along rotated X)
    params[3] = dy (Q12 step per grid cell along rotated Y)
    params[4] = phase (0..63)
    params[5] = fractal_type (0=Julia Biomorph, 1=Burning Ship, 2=Folded Mandala)
    params[6] = solid_mode (0=Ethereal GND drift, 1=Crisp White background hold)
    params[7] = gy_start (0..168)
    params[8] = gy_end (0..168)
    """
    cx = params[0]
    cy = params[1]
    dx = params[2]
    dy = params[3]
    phase = params[4]
    ftype = params[5]
    solid = params[6]
    gy = params[7]
    gy_end = params[8]

    while gy < gy_end:
        gx = 0
        while gx < 240:
            zx = (gx - 120) * dx - (gy - 84) * dy
            zy = (gx - 120) * dy + (gy - 84) * dx
            trap = 65535
            it = 0
            while it < 10:
                if ftype == 1:
                    if zx < 0:
                        zx = -zx
                    if zy < 0:
                        zy = -zy
                t = (zx * zx) >> 12
                u = (zy * zy) >> 12
                if t + u > 24576:
                    break
                if ftype == 2:
                    zy = ((zx * zy) >> 11) - (zy >> 2) + cy
                    zx = t - u + (zx >> 2) + cx
                else:
                    zy = ((zx * zy) >> 11) + cy
                    zx = t - u + cx
                u = (t + u) - 2800
                if u < 0:
                    u = -u
                u >>= 1
                t = zx if zx >= 0 else -zx
                if zy >= 0:
                    if zy < t:
                        t = zy
                else:
                    if -zy < t:
                        t = -zy
                if u < t:
                    t = u
                if t < trap:
                    trap = t
                it += 1

            t = ((trap >> 3) + it * 7 + phase) & 63
            if t < 12:
                u = 1  # Black filament
                it = 8 if (t < 2 or t >= 10) else (12 if (t == 2 or t == 9) else 16)
            elif t < 22:
                u = 2  # White clearing wave ahead of Red
                it = 16 if t < 19 else 12
            elif t < 38:
                u = 3  # Red orbit-trap stalk
                it = 12 if (t < 24 or t >= 35) else 16
            elif t < 46:
                u = 2  # Soft white mist edge
                it = 12 if t < 41 else 8
            else:
                if solid:
                    u = 2
                    it = 16
                else:
                    u = 0  # GND: let previous generations drift as multi-tone ink wash
                    it = 0

            trap = (gy * 480) + (gx >> 1)
            zx = 0
            while zx < 4:
                zy = dither[it + zx]
                if (gx & 1) == 0:
                    bw[trap] = (zy << 4) if (u & 1) else 0
                    rd[trap] = (zy << 4) if (u & 2) else 0
                else:
                    if u & 1:
                        bw[trap] |= zy
                    if u & 2:
                        rd[trap] |= zy
                trap += 120
                zx += 1

            gx += 1
        gy += 1


class BiomorphApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        super().__init__(name or "Biomorph")
        self.sleep_ms = 10
        self.launched = False

        # solid_mode=False (default): Ethereal ink-wash drift (Table 0 GND holds older layers)
        # solid_mode=True (UserA):    High-contrast crisp mode (Table 2 VDL bleaches background)
        self.solid_mode = False
        # fractal_type (UserB): 0 = Julia Biomorph, 1 = Burning Ship, 2 = Folded Vortex
        self.fractal_type = 0
        self.step = 0

        self._bw_cmd = None
        self._rd_cmd = None
        self._lut = None
        self._params = array.array("i", [0] * 9)

    def _ensure_buffers(self) -> None:
        if self._bw_cmd is None:
            fb_len = len(display._framebuffer_black)
            self._bw_cmd = bytearray(fb_len)
            self._rd_cmd = bytearray(fb_len)
            self._lut = _make_4rail_lut(_STEP_FRAMES)

    def _sync_leds(self) -> None:
        # Shimmering wave across the LED bar reflecting fractal_type and phase
        palettes = (
            ((0, 3, 5), (4, 0, 2)),  # Mode 0: Cyan / Magenta
            ((5, 1, 0), (3, 3, 0)),  # Mode 1: Ember / Gold
            ((0, 4, 2), (2, 0, 5)),  # Mode 2: Emerald / Violet
        )
        c1, c2 = palettes[self.fractal_type % 3]
        active = self.step % NUM_LEDS
        for i in range(NUM_LEDS):
            if i == active:
                leds[i] = c1
            elif ((i + self.step) & 3) == 0:
                leds[i] = c2
            elif self.solid_mode:
                leds[i] = (1, 1, 1)
            else:
                leds[i] = (0, 0, 0)
        leds.write()

    def _poll_inputs(self) -> None:
        if UserA.pressed():
            self.solid_mode = not self.solid_mode
            self._sync_leds()
        if UserB.pressed():
            self.fractal_type = (self.fractal_type + 1) % 3
            self._sync_leds()
        if Boot.pressed():
            # Jump ahead to a new region of the fractal orbit
            self.step += 17
            self._sync_leds()

    async def _poll_during(self, coro):
        task = aio.create_task(coro)
        while not task.done():
            self._poll_inputs()
            await aio.sleep_ms(5)
        self._poll_inputs()
        return await task

    def _compute_orbit(self) -> None:
        s = self.step
        # Smoothly orbit c(t) along the dendritic/spiral Julia boundary while breathing zoom & rotation
        th = 0.85 + s * 0.055
        if self.fractal_type == 0:
            # Cardioid-boundary spiral & Pickover biomorph region
            cx = int((-0.63 + 0.15 * math.cos(th)) * 4096)
            cy = int((0.35 + 0.21 * math.sin(th * 0.75)) * 4096)
            span_x = 1.85 + 0.30 * math.sin(s * 0.04)
        elif self.fractal_type == 1:
            # Burning Ship filaments
            cx = int((-0.52 + 0.18 * math.cos(th * 0.8)) * 4096)
            cy = int((-0.54 + 0.16 * math.sin(th * 0.65)) * 4096)
            span_x = 2.05 + 0.25 * math.cos(s * 0.05)
        else:
            # Folded Mandala vortex
            cx = int((-0.48 + 0.22 * math.sin(th * 0.7)) * 4096)
            cy = int((0.28 + 0.22 * math.cos(th * 0.9)) * 4096)
            span_x = 1.95 + 0.30 * math.sin(s * 0.045)

        rot = s * 0.035
        cell_step = (span_x * 4096.0) / 240.0
        self._params[0] = cx
        self._params[1] = cy
        self._params[2] = int(cell_step * math.cos(rot))
        self._params[3] = int(cell_step * math.sin(rot))
        # Advance phase by 3 per step so Black -> White Clear -> Red waves flow smoothly along stalks
        self._params[4] = (s * 3) & 63
        self._params[5] = self.fractal_type
        self._params[6] = 1 if self.solid_mode else 0

    async def _launch_draw(self) -> None:
        self._ensure_buffers()
        fast.max_images = 100_000
        fast.window_writes = False

        self.step = 0
        self._sync_leds()

        display.clear()
        await self._poll_during(
            fast.refresh(_FULL_WINDOW, pulls=3, keep_awake=True)
        )

    async def _step_fractal(self) -> None:
        self._compute_orbit()
        self._params[7] = 0
        self._params[8] = 84
        _render_fractal_frame(self._bw_cmd, self._rd_cmd, self._params, _DITHER_PATTERNS)
        self._poll_inputs()
        await aio.sleep_ms(0)

        self._params[6] = 1 if self.solid_mode else 0
        self._params[7] = 84
        self._params[8] = 168
        _render_fractal_frame(self._bw_cmd, self._rd_cmd, self._params, _DITHER_PATTERNS)
        self._sync_leds()

        await self._poll_during(
            fast.refresh(
                _FULL_WINDOW,
                black=self._bw_cmd,
                pulls=1,
                refresh_rows=None,
                old=self._rd_cmd,
                lut=self._lut,
                scan_rows=None,
                keep_awake=True,
            )
        )
        self.step += 1

    async def arun(self) -> None:
        if self.launched:
            await self._launch_draw()
            return

        self._poll_inputs()
        await self._step_fractal()
        await aio.sleep_ms(self.sleep_ms)
