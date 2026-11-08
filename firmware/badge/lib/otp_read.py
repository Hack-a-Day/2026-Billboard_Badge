"""Read and report the e-paper panel's OTP (one-time-programmable) fuse block.

The Pervasive Displays E2969JS0B1 (9.7" BWR, film J) stores factory-calibrated
register values and a DC/DC ramp sequence in an 88-byte OTP block.  The stock
driver hardcodes these values; this script reads the actual fuse data via the
0xB9 command so you can compare.

Usage (on the badge, from REPL):

    import asyncio
    from otp_read import read_otp
    asyncio.run(read_otp())

The OTP is read-only — this does not modify the panel.

Tested on a B1 panel at Supercon 2026 (Nov 8).  The read requires bit-banged
3-wire SPI because SDA (GPIO 47) must become an input for the data phase.
"""

import time
from machine import Pin, SPI

EPD_CS1_PIN = Pin(13, mode=Pin.OUT)
EPD_CS2_PIN = Pin(3, mode=Pin.OUT)
EPD_DC_PIN = Pin(10, mode=Pin.OUT)
EPD_RST_PIN = Pin(11, mode=Pin.OUT)
EPD_SCL_PIN = Pin(21, mode=Pin.OUT)
EPD_SDA_PIN = Pin(47, mode=Pin.OUT)
EPD_SPI_MISO_PIN = Pin(45, mode=Pin.IN)


def _make_spi():
    try:
        return SPI(1, baudrate=20_000_000,
                    sck=EPD_SCL_PIN, mosi=EPD_SDA_PIN, miso=EPD_SPI_MISO_PIN)
    except Exception:
        from machine import SoftSPI
        s = SoftSPI(baudrate=8_000_000,
                     sck=EPD_SCL_PIN, mosi=EPD_SDA_PIN, miso=EPD_SPI_MISO_PIN)
        s.init()
        return s


def _read_otp_raw(spi):
    """Read the 88-byte OTP block from the master COG via bit-banged 3-wire SPI."""
    spi.deinit()
    EPD_CS2_PIN.value(True)

    sda = Pin(47, mode=Pin.OUT)
    scl = EPD_SCL_PIN
    EPD_DC_PIN.value(False)
    EPD_CS1_PIN.value(False)
    cmd = 0xB9
    for i in range(7, -1, -1):
        sda.value((cmd >> i) & 1)
        time.sleep_us(1)
        scl.value(True)
        time.sleep_us(1)
        scl.value(False)
        time.sleep_us(1)

    time.sleep_ms(5)
    EPD_DC_PIN.value(True)
    sda_in = Pin(47, mode=Pin.IN)

    buf = bytearray(89)
    for j in range(89):
        byte = 0
        for i in range(7, -1, -1):
            scl.value(True)
            time.sleep_us(1)
            if sda_in.value():
                byte |= 1 << i
            scl.value(False)
            time.sleep_us(1)
        buf[j] = byte

    EPD_CS1_PIN.value(True)
    Pin(47, mode=Pin.OUT)
    return buf[1:]  # discard dummy byte


# B1 identification: reference doc section 4.3
def _is_b1(raw):
    if len(raw) < 88:
        return False
    if raw[0x16] != 0x3B or raw[0x0D] != 0x3B:
        return False
    if not (raw[0x28] & 0x80):
        return False
    if raw == bytearray(88) or raw == bytearray(b"\xff" * 88):
        return False
    return True


# B1 remap: reference doc Fig 4-2.  Fields from 0x12 sit one byte lower than
# canonical; the DC/DC ramp sits at 0x28 instead of 0x20.
def _remap_b1(raw):
    o = bytearray(88)
    o[:0x12] = raw[:0x12]
    o[0] = 0xA5
    o[0x12] = raw[0x12]
    o[0x13:0x1F] = raw[0x12:0x1E]
    o[0x20:0x40] = raw[0x28:0x48]
    return o


# Hardcoded values in the stock epd.py driver for comparison
_DEFAULTS = {
    "TCON 0x60":  (0x0B, bytes([0x25])),
    "DRFW 0x90":  (0x0C, bytes([0x00, 0x3B, 0x00, 0xA9])),
    "DCTL 0x01":  (0x10, bytes([0x09])),
    "VCOM 0x02":  (0x11, bytes([0x00])),
    "RAM_RW 0x12": (0x13, bytes([0x3B, 0x00, 0x14])),
    "DUW 0x13":   (0x16, bytes([0x00, 0x3B, 0x00, 0x00, 0x9F, 0x02])),
    "STV 0x61":   (0x1C, bytes([0x01])),
    "0xD8":       (0x1D, bytes([0x80])),
    "0xD6":       (0x1E, bytes([0x00])),
}


async def read_otp():
    """Read the panel OTP block, identify it, and compare with hardcoded values."""
    EPD_RST_PIN.value(False)
    time.sleep_ms(5)
    EPD_RST_PIN.value(True)
    time.sleep_ms(5)
    EPD_CS1_PIN.value(True)
    EPD_CS2_PIN.value(True)

    spi = _make_spi()
    raw = _read_otp_raw(spi)
    spi = _make_spi()

    print("OTP raw (%d bytes):" % len(raw))
    for i in range(0, len(raw), 16):
        line = raw[i:min(i + 16, len(raw))]
        print("  %02x: %s" % (i, " ".join("%02x" % b for b in line)))

    if not _is_b1(raw):
        print("\nNot identified as a B1 panel (or read failed).")
        print("Check: [0x16]=%02x [0x0D]=%02x [0x28]=%02x" % (
            raw[0x16], raw[0x0D], raw[0x28]))
        return

    otp = _remap_b1(raw)
    print("\nB1 panel confirmed. Remapped OTP:")
    for i in range(0, 88, 16):
        line = otp[i:min(i + 16, 88)]
        print("  %02x: %s" % (i, " ".join("%02x" % b for b in line)))

    print("\nRegister comparison (OTP vs driver hardcoded):")
    diffs = 0
    for name, (off, default) in _DEFAULTS.items():
        val = bytes(otp[off:off + len(default)])
        match = val == default
        mark = "  " if match else "!="
        print("  %s %-12s otp=%s  default=%s" % (
            mark, name, val.hex(), default.hex()))
        if not match:
            diffs += 1

    if diffs == 0:
        print("\nAll registers match the hardcoded defaults.")
    else:
        print("\n%d register(s) differ from the driver's hardcoded values." % diffs)

    print("\nDC/DC ramp stages (otp[0x20:0x40]):")
    for s in range(4):
        off = 0x20 + s * 8
        rec = otp[off:off + 8]
        fmt = "RAMPED" if rec[0] & 0x80 else "PLAIN"
        rep = rec[0] & 0x7F
        print("  stage %d: %s  %s rep=%d" % (s, rec.hex(), fmt, rep))

    # power down cleanly
    EPD_DC_PIN.value(False)
    EPD_CS1_PIN.value(False)
    EPD_RST_PIN.value(False)
    EPD_CS1_PIN.value(True)
    print("\nDone. Panel powered down.")
