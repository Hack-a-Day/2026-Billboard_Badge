"""Tone grid: DC balanced tone experiments for the B1 panel.

This is mostly an educational app that you can hack and use for something
cooler, like showing photos or a web browser or something.

TL;DR: I load a custom waveform that barely nudges the e-ink particles, 
then make tones by doing sequences of nudges.

A stock update draws a BWR reference strip above every cell, a BWR checker 
pattern in the cell, and the label. Then a custom nudge waveform is loaded 
and used to run a neutral erase/mix sequence, and draw test tones in the  
cell. If a pattern is not DC balanced, a balance prefix is added.

Outside of the tone area, the references and labels are held at GND and so do
not change. A ghost of the checker in the tone area shows if the erase pattern
wasn't enough to clear a previous image.

To make this easier to experiment and reason about, I use a notation for the
tone patterns by combining nudge components and repeat counts.

`I` for ink, `P` for paper, `R` for a red, and `r` for a half-red step. The 
combination and ordering cause the particles to arrange differently and create 
different tones on the display. So `I3` is 3 nudges towards black.

How far they move depends on the total time the field is applied and where 
the particles were. Because they are particles, they can bump into each 
other, cluster, or get stuck or trapped by other particles, or be somewhere 
in between.

Generally you want to avoid slamming the particles against a plate in any one 
direction for too long, or applying an unballanced waveform that would cause 
it to see net DC applied too often or for too long.

The stock OTP waveforms DC balance in the waveform, but this uses unbalanced
waveforms that only nudge, and balances in software by prefixing a sequence
with enough of the opposite charge and balances things across several updates.

Concept, design, direction, review, rewriting, and editing by Ben Hencke.
Code was written by a computer somewhere.
"""

import asyncio as aio
import framebuf
import sys

from apps.base_app import BaseApp
from lib.epd import display, EPD_BUSY1_PIN, EPD_BUSY2_PIN
from lib.led_bar import leds, NUM_LEDS

GND, VDH, VDL, VDHR = 0, 1, 2, 3

# The map of notation system characters to steps sent to the panel. 
# An update is one of: 'g' sends GND, hold. 'l' is 4 frames of VDL toward white
# 'h' is 4 frames of VDH, toward black. 'w' is 9 frames of VDHR toward red.
STEPS = {
    "H": "g",       # hold. net DC +0
    "P": "l",       # paper. net DC -4
    "I": "h",       # ink. net DC +4
    "R": "lww",     # red, the same ratio stock uses. net DC +4
    "r": "lw",      # half red: a paper kick, then one pull of red. net DC +0
}

# ERASE leaves a neutral tone, not white, with particles mixed up and 
# slightly distributed so that theya are easier to nudge. DC ballanced.
ERASE = "IIIIIPPPPPIPIPIPIPIPIPIPIPIPIPIPIPIPIPIPIP"

# Define patterns for the grid, using the notation. Repeat a character or
# append a number to repeat. 'II' == 'I2'
# 'I5R6' is 5 ink, then 6 red. Counter DC offset is added as a prefix
GRID = [
    # grays, black to mid gray. They end on ink to avoid pink
    ["IP2IP2IP2I10", "I3", "II", "I", "P4I", "P5I", "P6I", "P8I"],
    # light grays to white. The last two have a little pink
    ["P5I2P", "P5IP", "P8IP", "P8", "P5", "P3", "PP", "P4I2"],
    # dark reds and maroons, darkest first
    ["I5R2", "R3I", "R3rI", "R6I", "I5R6I", "I5R3", "I5R4", "R4PIR3"],
    # deep reds, then I5R4IR2 closely matches stock red. Then the darkest pinks
    ["R3IR3", "I5R4PIR3", "I5R4IR2", "I5R2r", "IR4PR3", "I5R6r", "RIR2", "IPR2"],
    # saturated pinks, dark to light
    ["I5R3r", "R3", "R3r", "I5R6Pr", "I5R3P", "I5R6P", "I5R6rP", "I5R6P2"],
    # pale pinks, and mixes of the 3 inks: including erase only (neutral BWR)
    ["R2P", "RP", "R2P2", "", "R2", "R2PI", "IPRPR", "R2I2P"],
]

HEADER = [
    "Calibration, labels, BWR checkerboard using OTP waveform. Then custom waveform erases the checkerboard",
    "in the tone area with 'I5 P5 (IP)x16', then plays a tone pattern.",
]

# Net DC of one update in VDL frames
NET = {"g": 0, "l": -4, "h": 4, "w": 4}

X0, Y0 = 10, 24             # start of grid, leaving room for some text
PITCH_X, PITCH_Y = 118, 108
CELL_W, CELL_H = 108, 68
REF_H = 16                  # stock color reference strip above the cell
TILE_W, TILE_H = 18, 17     # 6 across, 4 down
LABEL_CHARS = CELL_W // 8   # per line, two lines
HALF_W, HEIGHT = 480, 672
WHITE, BLACK, RED = 0, 1, 2  # stock colors
STOCK_UPDATE_MS = 32000     # the stock waveform plays 1072 frames, about 31 s


def expand(pattern):
    """Expand a pattern into frame updates, one STEPS character per
    update. A number repeats the previous character. I2R2 is the same as I I R R
    and gives "hhlwwlww"."""
    out = ""
    k = 0
    while k < len(pattern):
        character = pattern[k]
        k += 1
        digits = ""
        while k < len(pattern) and pattern[k].isdigit():
            digits += pattern[k]
            k += 1
        out += STEPS[character] * int(digits or "1")
    return out


def net(pattern):
    return sum(NET[c] for c in expand(pattern))


def balance_prefix(pattern):
    """The paper or ink run that cancels the net drive of the pattern, such
    as P12. It plays before the pattern."""
    count = round(net(pattern) / 4)     # one P or I is 4 frames
    if count > 0:
        return "P" + str(count)
    if count < 0:
        return "I" + str(-count)
    return ""


def cells():
    """x, y and pattern of every cell. y is the top of the reference strip."""
    for row, patterns in enumerate(GRID):
        for col, pattern in enumerate(patterns):
            yield X0 + col * PITCH_X, Y0 + row * PITCH_Y, pattern


def make_planes():
    """Buffers in _send_images() order: black left, red left, black right,
    red right."""
    bufs = [bytearray(HALF_W * HEIGHT // 8) for _ in range(4)]
    fbs = [framebuf.FrameBuffer(b, HALF_W, HEIGHT, framebuf.MONO_HMSB) for b in bufs]
    return bufs, fbs


def rect(fbs, x, y, w, h, value):
    """Fills a rectangle with a 2 bit value across both panel halves."""
    fbs[0].fill_rect(x, y, w, h, value & 1)
    fbs[1].fill_rect(x, y, w, h, value >> 1)
    fbs[2].fill_rect(x - HALF_W, y, w, h, value & 1)
    fbs[3].fill_rect(x - HALF_W, y, w, h, value >> 1)


def text(fbs, string, x, y):
    """Black text for the stock update, drawn into both halves."""
    fbs[0].text(string, x, y, 1)
    fbs[2].text(string, x - HALF_W, y, 1)


def draw_references(fbs):
    for fb in fbs:
        fb.fill(0)
    text(fbs, HEADER[0], 0, 2)
    text(fbs, HEADER[1], 0, 11)
    third = CELL_W // 3
    for x, y, pattern in cells():
        # reference colors
        rect(fbs, x, y, third, REF_H, BLACK)
        rect(fbs, x + 2 * third, y, third, REF_H, RED)
        # draw checkerboard for ghost testing.
        for tile_row in range(CELL_H // TILE_H):
            for tile_col in range(CELL_W // TILE_W):
                color = (WHITE, BLACK, RED)[(tile_row + tile_col) % 3]
                rect(fbs, x + tile_col * TILE_W, y + REF_H + tile_row * TILE_H, TILE_W, TILE_H, color)
        label = balance_prefix(pattern) + " " + pattern
        if pattern == "":
            label = "erase only"
        label_y = y + REF_H + CELL_H + 3
        text(fbs, label[:LABEL_CHARS], x, label_y)
        text(fbs, label[LABEL_CHARS:], x, label_y + 9)


def sequences():
    """x, y and every character each cell plays after the erase: balance
    prefix, pattern. Made once for a run, because expanding 48 patterns
    takes time."""
    out = []
    for x, y, pattern in cells():
        sequence = expand(balance_prefix(pattern) + pattern)
        out.append((x, y, sequence))
    return out


def draw_erase_step(fbs, character):
    """One erase update in every cell."""
    for fb in fbs:
        fb.fill(0)
    for x, y, _ in cells():
        rect(fbs, x, y + REF_H, CELL_W, CELL_H, "glhw".index(character))


def draw_step(fbs, played, k):
    """Draw cells for update k, bitmap/waveform based on sequence."""
    for fb in fbs:
        fb.fill(0)
    for x, y, sequence in played:
        if k < len(sequence):
            rect(fbs, x, y + REF_H, CELL_W, CELL_H, "glhw".index(sequence[k]))


def tone_table():
    """Create the custom nudge waveform table for g, l, h, w steps."""
    # haven't found a way to have less than 10 groups, or less than 1 phase 
    # frame per group, so this is 10 frames.
    waveforms = [
        [GND] * 10,                 # 00 g
        [VDL] * 4 + [GND] * 6,      # 01 l
        [VDH] * 4 + [GND] * 6,      # 10 h
        [VDHR] * 9 + [GND],         # 11 w
        [GND] * 10,                 # VCOM
    ]
    out = bytearray()
    for waveform in waveforms:
        for voltage in waveform:
            out += nudge_group(voltage)
    return out


def nudge_group(voltage):
    """A simplified group: one 8 byte group record of the 0xB0 table that
    plays one frame of voltage in phase C. Phases A and B are skipped,
    phase D plays no frames."""
    phase_levels = voltage << 2     # 2 bits per phase, A in bits 0 and 1, then B, C, D
    ta, tb, sr_ab = 1, 1, 0         # frames of A and B. Repeat 0 skips them
    tc, td, sr_cd = 1, 0, 1         # one frame of C, none of D, once
    rp = 1                          # the group plays once
    return bytes([phase_levels, ta, tb, sr_ab, tc, td, sr_cd, rp])


def idle():
    return EPD_BUSY1_PIN.value() and EPD_BUSY2_PIN.value()


async def update():
    await display._display_refresh()    # waits for idle, sends the trigger
    for _ in range(200):                # busy goes low when the update starts
        if not idle():
            break
        await aio.sleep_ms(1)
    while not idle():
        await aio.sleep_ms(5)


def progress(color, done, total):
    """Light the LED bar from the left, done out of total, in color. One LED
    is lit from the start, all of them when done."""
    lit = max(1, NUM_LEDS * done // total)
    for i in range(NUM_LEDS):
        leds[i] = color if i < lit else (0, 0, 0)
    leds.write()


async def run_grid():
    if not display._reverse_fb_bits:    # the driver sets this only for B1
        print("B1 panel only. Send Hencke a badge with an '84 if you want support.")
        return
    longest = 0
    for _, _, pattern in cells():
        played = balance_prefix(pattern) + pattern
        print("prefix", balance_prefix(pattern), "pattern", pattern, "net left", net(played))
        longest = max(longest, len(expand(played)))
    while display._framebuffer_ready.is_set() or not display.is_done_updating():
        await aio.sleep_ms(5)
    bufs, fbs = make_planes()
    reverse = display._reverse_fb_bits
    display._reverse_fb_bits = False
    try:
        draw_references(fbs)
        await display._reset()
        display._send_images(*bufs)
        await display.cog_initial()
        await display._dcdc_soft_start()
        await display._display_refresh()
        polls = STOCK_UPDATE_MS // 250
        for k in range(polls):
            progress((8, 0, 0), k + 1, polls)
            await aio.sleep_ms(250)
            if idle():
                break
        await display._dcdc_soft_shutdown()

        erase = expand(ERASE)
        played = sequences()
        await display._reset()
        await display.cog_initial()
        display._send_index_data(0xB0, tone_table())
        await display._dcdc_soft_start()
        for k in range(len(erase)):
            print("erase", k + 1, "of", len(erase))
            progress((0, 0, 8), k + 1, len(erase))      # blue
            draw_erase_step(fbs, erase[k])
            display._send_images(*bufs)
            await update()
        for k in range(longest):
            print("step", k + 1, "of", longest)
            progress((0, 8, 0), k + 1, longest)         # green
            draw_step(fbs, played, k)
            display._send_images(*bufs)
            await update()
        await display._dcdc_soft_shutdown()
    finally:
        display._reverse_fb_bits = reverse
        leds.clear()


class ToneGridApp(BaseApp):
    def __init__(self):
        super().__init__("Tone Grid")

    async def arun(self):
        if self.launched:
            try:
                await run_grid()
            except Exception as ex:
                sys.print_exception(ex)
        await aio.sleep_ms(self.sleep_ms)
