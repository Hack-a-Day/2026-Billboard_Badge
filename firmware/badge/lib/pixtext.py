"""Big blocky text for the e-paper, scaled up from the built-in 8x8 font.

Draws straight into a framebuf plane (display.black / display.red), so it
needs no LVGL and no image files.
"""

import framebuf

_buf = bytearray(8)
_glyph = framebuf.FrameBuffer(_buf, 8, 8, framebuf.MONO_HLSB)


def width(s, scale):
    return len(s) * 8 * scale


def text(plane, s, x, y, scale=4, color=1):
    """Draw s at pixel position (x, y); each font pixel becomes scale x scale."""
    for ch in s:
        _glyph.fill(0)
        _glyph.text(ch, 0, 0, 1)
        for gy in range(8):
            row = _buf[gy]  # MONO_HLSB: leftmost pixel is the top bit
            gx = 0
            while gx < 8:
                if row & (0x80 >> gx):
                    run = gx
                    while run < 8 and row & (0x80 >> run):
                        run += 1
                    plane.fill_rect(
                        x + gx * scale, y + gy * scale,
                        (run - gx) * scale, scale, color,
                    )
                    gx = run
                else:
                    gx += 1
        x += 8 * scale


def center(plane, s, y, scale=4, color=1, total_width=960):
    text(plane, s, (total_width - width(s, scale)) // 2, y, scale, color)
