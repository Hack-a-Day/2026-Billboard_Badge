"""Tetris Tag. Tetris-like pieces fall and stack up to spell a name along the
bottom edge of the screen, the name blinks, stays up for HOLD_S seconds, then it is
built again (or the next name, if NAMES lists more than one).

User A: turn the left falling piece 90 degrees clockwise
User B: turn the right falling piece 90 degrees clockwise
(between names, User A skips to the next name and User B restarts the current one)

A red banner with white Helvetica Bold lettering and the two players' score boards runs
across the top of the screen, and a red footer across the bottom 25% (your web address or
social handles); the name stands above it.
The lettering is an image made on a computer by make_banner.py. Edit NAMES and the
settings below to make it your own.

The panel's own refresh takes about 30 s, far too slow to animate. On B1 panels with
lib/epd_partial.py (about 0.65 s a frame) two pieces fall at a time, like a two-player
Tetris, in partial refreshes: the screen is cleared with one full refresh that also draws
the red bands, then only the pixels that change are redrawn. Both pieces appear
at 25% of the screen height turned a random quarter turn and fall without turning.
User A turns the left piece and User B the right one, 90 degrees clockwise per press.
Once a piece is the right way up it drops into place on the next refresh. A piece
nobody has turned starts turning itself when it reaches 50% of the screen height.
Elsewhere the app falls back to a slideshow, one full refresh per snapshot, showing
the pieces landed (black) and the next few still in the air (red).
"""

import asyncio as aio
import framebuf
import gc
import random
import sys
import time

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.epd import display, display_width, display_height
from lib.led_bar import leds

try:  # the partial refresh driver; without it the app falls back to the slideshow
    from lib import epd_partial as _ep
except ImportError:
    _ep = None

NAMES = ("YOUR NAME",)
"""Shown in turn, forever. Replace "YOUR NAME" with your own, using only the characters
in FONT below (the letters A to Z and a to z, é, and space), or add more names to the
tuple to cycle through them. Punctuation needs a 5 x 7 glyph of its own."""

BANNER_IMAGE = "/data/graphics/tetris_banner.png"
"""White lettering on red, 960 px wide, made on a computer by make_banner.py and
copied to the badge (the badge has no Helvetica, and its built-in font has neither
Cyrillic nor the hammer and sickle). The lettering sits in the middle, low in the band,
between and under the score boards (see SCOREBOARD):
    python3 make_banner.py --pct 25 --margin 190 --height-fraction 0.42 --drop 18
If the file is missing, the banner is a plain red band."""

BANNER_PCT = 25
"""Banner height as a percent of the screen height. 0 for no banner. Must match the
image (make_banner.py --pct). The banner is red, so it is drawn by the one full refresh
when the app is launched, and the partial refreshes leave it untouched."""

TARGET_FRAMES = 8
"""Roughly how many e-paper refreshes it takes to build one name.
More frames is smoother, but each one costs a full refresh."""

MAX_CELL = 16
"""Largest block size in pixels. Names are scaled down to fit the width."""

BOTTOM_MARGIN = 8
"""Pixels left clear between the letters and the footer band (or the bottom of the
screen, with no footer)."""

NAME_RISE_PCT = 6.55
"""How far the name is lifted above that position, as a percent of the screen height
(6.55 is 44 px). The pieces then have that much less to fall."""

FOOTER_PCT = 25
"""Height of the red footer band across the bottom, as a percent of the screen height.
It is drawn by the one full refresh when the app is launched and stays on the screen all
the time; the name lands right above it. 0 for no footer."""

FOOTER_IMAGE = "/data/graphics/tetris_footer.png"
"""White lettering on red, 960 px wide, for your web address or social handles, centred in
the band. Make it on a computer with make_banner.py, at the same font size as the top
banner's lettering (78 px):
    python3 make_banner.py --text "web / @socials" --pct 25 --size 78 --out tetris_footer.png
and copy it to the badge (see the README). If the file is missing the footer is a plain
red band."""

SCOREBOARD = True
"""Show a score board for each player at the two ends of the top banner (SCORE, LINES, the
NEXT piece and which button, User A on the left and User B on the right) and a timer, MM:SS,
at the top centre. Needs the banner to be the full 25%. A letter finished is a line; 10 points
for a piece landed, 10 more if the player turned it right way up before it started turning
itself, 100 for the piece that finishes a letter. The panels are red with white lettering;
the numbers and the next piece are black in small white boxes and the timer is white on a
black box, because only white and black can be changed by a partial refresh."""

BLINK_MODE = "leds"
"""'leds':   the finished name stays on the paper and the LED bar blinks.
This is the only way to get a real half-second on/off, since the paper cannot
refresh that quickly.
'screen': blink the paper itself, name -> blank -> name. Each step waits for a
full refresh to finish, so it is slow and flashes the whole panel."""

BLINK_COUNT = 3
BLINK_MS = 500
BLINK_COLOR = (20, 20, 20)
"""LED brightness is 0-255 and they are bright. Keep it low."""

HOLD_S = 30
"""Seconds the finished name stays on the screen, after the blinking, before the next
round starts. User A skips the wait to the next name, User B restarts this one."""

FAST = True
"""Use partial refreshes (lib/epd_partial.py) where the panel and firmware support
them. False forces the slideshow."""

PIECES_AT_ONCE = 2
"""Pieces falling at the same time: 1 or 2. With 2, User A turns the left one and User
B the right one (with 1, either button turns it). Two pieces only fall together if
they land in separate columns, and each stays on its own side of the screen while it
is in the air."""

SPAWN_PCT = 25
"""Every piece appears at this percent of the screen height, measured from the top.
25 is the bottom edge of the banner. It is never put inside the banner."""

FALL_PX = 36
"""How far, in pixels, a piece falls in each refresh while it is turning or waiting to be
turned (rounded to whole block rows: 3 rows for 12 px blocks, 2 for 16 px blocks). Once it
is the right way up it falls the rest of the way into place in one refresh. Raise it for a
faster game, but the time a player has to press a button before the piece turns itself
shrinks in the same proportion."""

MIN_PRESS_FRAMES = 3
"""The fall slows down, if it has to, until a player has at least this many refreshes to turn
a piece before it starts turning itself (a short play field leaves little time). It never
falls less than one row a refresh."""

AUTO_TURN_PCT = 50
"""A piece that is still not the right way up turns itself, 90 degrees a refresh by the
shorter way round, once it reaches this percent of the screen height. Buttons only turn
a piece above this line. The line is never lower than the pieces need to finish turning
above the name, so with the footer it ends up higher than this."""

FLIGHT_MIN_ROWS = 2
"""Slideshow only: pieces in the air are drawn at least this many rows above
their landing spot."""

SPLIT_ATTEMPTS = 10
"""Random tries at cutting each letter into pieces. Fewer pieces is better."""

GLYPH_ROWS = 7

FONT = {
    " ": ("...",) * GLYPH_ROWS,
    "A": (".XXX.", "X...X", "X...X", "XXXXX", "X...X", "X...X", "X...X"),
    "B": ("XXXX.", "X...X", "X...X", "XXXX.", "X...X", "X...X", "XXXX."),
    "C": (".XXX.", "X...X", "X....", "X....", "X....", "X...X", ".XXX."),
    "D": ("XXXX.", "X...X", "X...X", "X...X", "X...X", "X...X", "XXXX."),
    "E": ("XXXXX", "X....", "X....", "XXXX.", "X....", "X....", "XXXXX"),
    "F": ("XXXXX", "X....", "XXXX.", "X....", "X....", "X....", "X...."),
    "G": (".XXXX", "X....", "X....", "X.XXX", "X...X", "X...X", ".XXXX"),
    "H": ("X...X", "X...X", "X...X", "XXXXX", "X...X", "X...X", "X...X"),
    "I": ("XXX", ".X.", ".X.", ".X.", ".X.", ".X.", "XXX"),
    "J": ("..XXX", "...X.", "...X.", "...X.", "...X.", "X..X.", ".XX.."),
    "K": ("X...X", "X..X.", "X.X..", "XX...", "X.X..", "X..X.", "X...X"),
    "L": ("X....", "X....", "X....", "X....", "X....", "X....", "XXXXX"),
    "M": ("X...X", "XX.XX", "X.X.X", "X.X.X", "X...X", "X...X", "X...X"),
    "N": ("X...X", "X...X", "XX..X", "X.X.X", "X..XX", "X...X", "X...X"),
    "O": (".XXX.", "X...X", "X...X", "X...X", "X...X", "X...X", ".XXX."),
    "P": ("XXXX.", "X...X", "X...X", "XXXX.", "X....", "X....", "X...."),
    "Q": (".XXX.", "X...X", "X...X", "X...X", "X.X.X", "X..X.", ".XX.X"),
    "R": ("XXXX.", "X...X", "X...X", "XXXX.", "X.X..", "X..X.", "X...X"),
    "S": (".XXXX", "X....", "X....", ".XXX.", "....X", "....X", "XXXX."),
    "T": ("XXXXX", "..X..", "..X..", "..X..", "..X..", "..X..", "..X.."),
    "U": ("X...X", "X...X", "X...X", "X...X", "X...X", "X...X", ".XXX."),
    "V": ("X...X", "X...X", "X...X", "X...X", "X...X", ".X.X.", "..X.."),
    "W": ("X...X", "X...X", "X...X", "X.X.X", "X.X.X", "XX.XX", "X...X"),
    "X": ("X...X", "X...X", ".X.X.", "..X..", ".X.X.", "X...X", "X...X"),
    "Y": ("X...X", "X...X", ".X.X.", "..X..", "..X..", "..X..", "..X.."),
    "Z": ("XXXXX", "....X", "...X.", "..X..", ".X...", "X....", "XXXXX"),
    # Lowercase letters have no descender, so every letter sits on the same floor.
    "a": (".....", ".....", ".XXX.", "....X", ".XXXX", "X...X", ".XXXX"),
    "b": ("X....", "X....", "XXXX.", "X...X", "X...X", "X...X", "XXXX."),
    "c": (".....", ".....", ".XXXX", "X....", "X....", "X....", ".XXXX"),
    "d": ("....X", "....X", ".XXXX", "X...X", "X...X", "X...X", ".XXXX"),
    "e": (".....", ".....", ".XXX.", "X...X", "XXXXX", "X....", ".XXXX"),
    # Acute accent in the two rows above the x-height.
    "é": ("...X.", "..X..", ".XXX.", "X...X", "XXXXX", "X....", ".XXXX"),
    "f": ("..XX.", ".X...", "XXXX.", ".X...", ".X...", ".X...", ".X..."),
    "g": (".....", ".....", ".XXXX", "X...X", ".XXXX", "....X", ".XXX."),
    "h": ("X....", "X....", "X.XX.", "XX..X", "X...X", "X...X", "X...X"),
    "i": (".X.", "...", "XX.", ".X.", ".X.", ".X.", "XXX"),
    "j": ("...X.", ".....", "...X.", "...X.", "...X.", "X..X.", ".XX.."),
    "k": ("X....", "X....", "X..X.", "X.X..", "XX...", "X.X..", "X..X."),
    "l": ("XX.", ".X.", ".X.", ".X.", ".X.", ".X.", "XXX"),
    "m": (".....", ".....", "XX.X.", "X.X.X", "X.X.X", "X.X.X", "X.X.X"),
    "n": (".....", ".....", "X.XX.", "XX..X", "X...X", "X...X", "X...X"),
    "o": (".....", ".....", ".XXX.", "X...X", "X...X", "X...X", ".XXX."),
    "p": (".....", ".....", "XXXX.", "X...X", "XXXX.", "X....", "X...."),
    "q": (".....", ".....", ".XXXX", "X...X", ".XXXX", "....X", "....X"),
    "r": (".....", ".....", "X.XX.", "XX..X", "X....", "X....", "X...."),
    "s": (".....", ".....", ".XXXX", "X....", ".XXX.", "....X", "XXXX."),
    "t": (".X...", ".X...", "XXXX.", ".X...", ".X...", ".X..X", "..XX."),
    "u": (".....", ".....", "X...X", "X...X", "X...X", "X...X", ".XXXX"),
    "v": (".....", ".....", "X...X", "X...X", "X...X", ".X.X.", "..X.."),
    "w": (".....", ".....", "X...X", "X...X", "X.X.X", "X.X.X", ".X.X."),
    "x": (".....", ".....", "X...X", ".X.X.", "..X..", ".X.X.", "X...X"),
    "y": (".....", ".....", "X...X", "X...X", ".XXXX", "....X", ".XXX."),
    "z": (".....", ".....", "XXXXX", "...X.", "..X..", ".X...", "XXXXX"),
    "0": (".XXX.", "X...X", "X..XX", "X.X.X", "XX..X", "X...X", ".XXX."),
    "1": ("..X..", ".XX..", "..X..", "..X..", "..X..", "..X..", ".XXX."),
    "2": (".XXX.", "X...X", "....X", "...X.", "..X..", ".X...", "XXXXX"),
    "3": (".XXX.", "X...X", "....X", "..XX.", "....X", "X...X", ".XXX."),
    "4": ("...X.", "..XX.", ".X.X.", "X..X.", "XXXXX", "...X.", "...X."),
    "5": ("XXXXX", "X....", "XXXX.", "....X", "....X", "X...X", ".XXX."),
    "6": (".XXX.", "X....", "X....", "XXXX.", "X...X", "X...X", ".XXX."),
    "7": ("XXXXX", "....X", "...X.", "..X..", ".X...", ".X...", ".X..."),
    "8": (".XXX.", "X...X", "X...X", ".XXX.", "X...X", "X...X", ".XXX."),
    "9": (".XXX.", "X...X", "X...X", ".XXXX", "....X", "....X", ".XXX."),
    ":": ("...", "...", ".X.", "...", ".X.", "...", "..."),
}

_BUILD = 0
_BLINK = 1
_CLEAR = 2
_DROP = 3
_HOLD = 4

_FULL = 1  # what the screen task is asked to do: a normal refresh of the whole panel,
_PART = 2  # or a partial one of what changed
_ALIVE_MS = 2500  # the screen task gives the panel back this long after run() stops
_IDLE_MS = 2000   # and powers it down this long after the picture stops changing


if _ep is not None:
    class _Panel(_ep.PartialEPD):
        """PartialEPD for a screen that has red on it. start() wipes the panel to white,
        which would also wipe the red bands; this opens a session on top of the picture
        a normal refresh has just drawn instead. Black pixels the partial pulls never
        touch, and so the red under them, stay as they are."""

        async def start_keep(self, film):
            """Open a session. film is the black plane the last normal refresh drew."""
            d = display
            if not d._reverse_fb_bits:
                raise _ep.PanelError("partial refresh is only known to work on the B1 panel")
            while d._framebuffer_ready.is_set() or not d.is_done_updating():
                await aio.sleep_ms(50)
            lock = getattr(d, "lock", None)
            if lock is not None:
                await lock.acquire()
                self._locked = True
            try:
                if self._rst():  # chips awake, maybe still refreshing: never reset mid-refresh
                    await self._ready("idle", 60_000)
                self._spi = d.spi
                if self.buf is None:
                    # (epd_partial's own sizes are const()s with a leading underscore, which
                    # MicroPython does not export from the module.)
                    plane = display_width * display_height // 8
                    self.buf = bytearray(plane)
                    self.fb = framebuf.FrameBuffer(self.buf, display_width, display_height,
                                                   framebuf.MONO_HLSB)
                    self._shown = bytearray(plane)
                    self._stage = bytearray(plane // 2)
                memoryview(self._shown)[:] = film
                memoryview(self.buf)[:] = film
                await self._open()
            except BaseException:
                await self.stop()
                raise


def _neighbors(c):
    x, y = c
    return ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))


def _around(c):
    """The eight cells around c, the four that share an edge first."""
    x, y = c
    return _neighbors(c) + ((x + 1, y + 1), (x - 1, y - 1), (x + 1, y - 1), (x - 1, y + 1))


def _text_cells(text):
    """The cells (x, y) of each letter on a grid GLYPH_ROWS tall, and the text width."""
    glyphs = []
    x = 0
    for ch in text:
        rows = FONT[ch]
        cells = []
        for y in range(GLYPH_ROWS):
            for gx, mark in enumerate(rows[y]):
                if mark == "X":
                    cells.append((x + gx, y))
        if cells:
            glyphs.append(cells)
        x += len(rows[0]) + 1
    return glyphs, x - 1


def _greedy_split(cells):
    """Cut cells into pieces of up to four, hardest-to-fill cells first. A piece prefers
    cells that share an edge, but may reach across a corner, so a diagonal stroke can be
    cut into pieces of two."""
    free = set(cells)
    pieces = []
    while free:
        best = None
        best_n = 9
        for c in free:
            n = sum(1 for nb in _around(c) if nb in free)
            if n < best_n or (n == best_n and random.randrange(2)):
                best, best_n = c, n
        piece = [best]
        free.discard(best)
        while len(piece) < 4:
            options = []
            for pc in piece:
                for nb in _neighbors(pc):
                    if nb in free and nb not in options:
                        options.append(nb)
            if not options:
                for pc in piece:
                    for nb in _around(pc)[4:]:
                        if nb in free and nb not in options:
                            options.append(nb)
            if not options:
                break
            nxt = random.choice(options)
            piece.append(nxt)
            free.discard(nxt)
        pieces.append(piece)
    return _absorb_singles(pieces)


def _connected(cells, corners=True):
    """True if the cells touch as one group, by edges only unless corners is True."""
    rest = set(cells)
    stack = [rest.pop()]
    while stack:
        c = stack.pop()
        for nb in (_around(c) if corners else _neighbors(c)):
            if nb in rest:
                rest.discard(nb)
                stack.append(nb)
    return not rest


def _absorb_singles(pieces):
    """Fold lone cells into a piece next to them (edge or corner): join one that has room,
    or take a cell off a bigger one that stays whole without it. A cell with nothing
    around it stays alone."""
    changed = True
    while changed:
        changed = False
        for single in [p for p in pieces if len(p) == 1]:
            cell = single[0]
            for nb in _around(cell):
                other = None
                for q in pieces:
                    if nb in q and q is not single:
                        other = q
                        break
                if other is None:
                    continue
                if len(other) < 4:
                    other.append(cell)
                    pieces.remove(single)
                elif len(other) > 2 and _connected([c for c in other if c != nb]):
                    other.remove(nb)
                    single.append(nb)
                else:
                    continue
                changed = True
                break
            if changed:
                break
    return pieces


def _land_order(pieces):
    """What has to land before what, so every piece can fall straight down.
    Returns a list of sets, [i] being the pieces that must land before piece i,
    or None if the pieces block each other."""
    columns = {}
    for i, piece in enumerate(pieces):
        for x, y in piece:
            columns.setdefault(x, []).append((y, i))
    before = [set() for _ in pieces]
    for col in columns.values():
        for y_a, a in col:
            for y_b, b in col:
                if a != b and y_a > y_b:
                    # b is above a in this column, so a lands first.
                    before[b].add(a)
    # Cycle check: peel off pieces whose requirements are all met.
    done = set()
    while len(done) < len(pieces):
        ready = [i for i in range(len(pieces)) if i not in done and before[i] <= done]
        if not ready:
            return None
        done.update(ready)
    return before


def _split_glyph(cells):
    best = None
    best_score = None
    for _ in range(SPLIT_ATTEMPTS):
        pieces = _greedy_split(cells)
        # Fewest single blocks first, then fewest pieces held together only by a corner
        # (not like a real Tetris piece), then fewest pieces.
        score = (sum(1 for p in pieces if len(p) == 1),
                 sum(1 for p in pieces if not _connected(p, False)),
                 len(pieces))
        if best is not None and score >= best_score:
            continue
        before = _land_order(pieces)
        if before is not None:
            best = (pieces, before)
            best_score = score
    if best is None:
        # Single blocks can always be stacked bottom-up.
        pieces = [[c] for c in cells]
        best = (pieces, _land_order(pieces))
    return best


def _turn(shape):
    """A list of (x, y) cells turned a quarter turn clockwise, moved back to the origin."""
    turned = [(-y, x) for x, y in shape]
    left = min(x for x, _ in turned)
    top = min(y for _, y in turned)
    return sorted((x - left, y - top) for x, y in turned)


class _Flier:
    """A piece on its way down: turned some way, in its own columns, at some row."""

    def __init__(self, plan, i, lane, side):
        cells = plan.pieces[i]
        self.plan = plan
        self.i = i
        self.lane = lane
        self.side = side
        self.left = min(x for x, _ in cells)
        self.top = min(y for _, y in cells)
        final = sorted((x - self.left, y - self.top) for x, y in cells)
        self.width = max(x for x, _ in final) + 1
        self.shapes = [final]  # shapes[n]: the landed shape turned n quarter turns clockwise
        for _ in range(3):
            self.shapes.append(_turn(self.shapes[-1]))
        self.rot = random.randint(1, 3)
        self.way = random.choice((1, -1))  # which way to turn when both ways are as short
        self.rowf = float(plan.spawn_row)  # a float: the fall can be a fraction of a row
        self.row = plan.spawn_row
        self.arrived = False  # at its place, still an outline
        self.landed = False  # and solid black, one refresh later
        self.touched = False  # its player turned it
        self.auto = False  # it had to turn itself
        self.cells = self._place()

    def upright(self):
        return self.shapes[self.rot] == self.shapes[0]

    def _place(self):
        """Cells at the current turn and row, centred over the landing spot but kept
        inside this piece's lane."""
        shape = self.shapes[self.rot]
        w = max(x for x, _ in shape) + 1
        x = max(self.lane[0], min(self.left + (self.width - w) // 2, self.lane[1] - w))
        return [(x + sx, self.row + sy) for sx, sy in shape]

    def _auto_turn(self):
        """+1 or -1: the shorter way round to the right way up."""
        cw = 1
        while self.shapes[(self.rot + cw) % 4] != self.shapes[0]:
            cw += 1
        ccw = 1
        while self.shapes[(self.rot - ccw) % 4] != self.shapes[0]:
            ccw += 1
        if cw == ccw:
            return self.way
        return 1 if cw < ccw else -1

    def step(self, turns, auto_row):
        """Move on one refresh. A piece that is the right way up drops into place and stays an
        outline for one refresh (arrived), then turns solid black (landed); otherwise it falls
        plan.fall_rows, turned by the player's presses if it is still above auto_row, and by
        itself once it has fallen to auto_row."""
        if self.landed:
            return
        if self.arrived:
            self.landed = True
            return
        if self.upright():
            self.arrived = True
            self.cells = list(self.plan.pieces[self.i])
            return
        if turns and self.row < auto_row:
            self.rot = (self.rot + turns) % 4
            self.touched = True
        self.rowf = min(self.rowf + self.plan.fall_rows, self.top)
        self.row = int(self.rowf)
        if not self.upright() and self.row >= auto_row:
            self.rot = (self.rot + self._auto_turn()) % 4
            self.auto = True
        self.cells = self._place()


def _text(plane, s, x, y, cell, value=1):
    """Draw the string with the block font, each block `cell` pixels (may be fractional:
    4.5 gives blocks of 4 and 5 pixels). value 1 sets the pixels of the plane, 0 clears them:
    black digits on the black plane, white lettering by clearing the red plane."""
    pen = x  # a float when cell is: the pixel positions are rounded from it
    for ch in s:
        rows = FONT.get(ch)
        if rows is None:
            continue
        n = len(rows[0])
        for gy in range(GLYPH_ROWS):
            row = rows[gy]
            y0 = int(y + gy * cell + 0.5)
            high = int(y + (gy + 1) * cell + 0.5) - y0
            gx = 0
            while gx < n:
                if row[gx] == "X":
                    end = gx
                    while end < n and row[end] == "X":
                        end += 1
                    x0 = int(pen + gx * cell + 0.5)
                    plane.fill_rect(x0, y0, int(pen + end * cell + 0.5) - x0, high, value)
                    gx = end
                else:
                    gx += 1
        pen += (n + 1) * cell


def _disc(plane, cx, cy, r, value):
    """A filled circle of radius r (2 r + 1 pixels across), as rows of fill_rect."""
    for dy in range(-r, r + 1):
        half = int(((r + 0.5) ** 2 - dy * dy) ** 0.5)
        plane.fill_rect(cx - half, cy + dy, 2 * half + 1, 1, value)


_HUD_W = 149  # how wide a player's panel is
_HUD_H = 152
_PANEL_MARGIN = 6  # of the left panel from the left edge
_RIGHT_PANEL_SHIFT = 12  # the right panel sits this much further in from the right edge: its outer
# margin then equals the left panel's (14 px)
_TIMER_W = 140  # the box of the timer (black, with white digits)
_TIMER_H = 42
_TIMER_CELL = 4.5  # block size of the timer digits: 50% larger than the scores' 3
# Where the SCORE/LINES column and the NEXT/USER column start inside a panel. The left panel
# (User A) has NEXT/USER first; the right panel (User B) has SCORE/LINES first. 19 px apart.
_SCORE_DX = (69, 8)
_NEXT_DX = (8, 107)
_BTN_R = 8  # radius of the button circles beside the big A or B
_DOT_R = 4  # radius of the black dot shown while the button is down (9 px across, was 7)
_BTN_DX = (8, 36)  # where their centres are in the column: left of the A, right of the B
_LETTER_DX = (21, 0)  # and where the A or B itself starts


def _clock(seconds):
    """MM:SS (minutes stop at 99)."""
    return "%02d:%02d" % (min(seconds // 60, 99), seconds % 60)


class _Hud:
    """The score boards in the top banner: a panel for each player at its end and a timer
    at the top centre. Red can only be drawn by the full refresh, so whatever changes has
    to sit on white or black: the panels are the red band with white lettering, the numbers
    and the next piece are black in small white boxes and the timer is white on black. Partial refreshes change only those
    boxes."""

    def __init__(self, top):
        self.xs = (_PANEL_MARGIN, display_width - _PANEL_MARGIN - _HUD_W - _RIGHT_PANEL_SHIFT)
        self.y = top + 8
        self.tx = (display_width - _TIMER_W) // 2
        self.ty = top + 6

    def boxes(self):
        """The white boxes that change, as (x, y, width, height)."""
        out = []
        for side in (0, 1):
            x, sx, nx = self.xs[side], _SCORE_DX[side], _NEXT_DX[side]
            out += [(x + sx, self.y + 28, 80, 29), (x + sx, self.y + 82, 44, 29),
                    (x + nx, self.y + 28, 42, 42)]
        out.append((self.tx, self.ty, _TIMER_W, _TIMER_H))
        return out

    def wells(self):
        """Everything that may change after the full refresh: the boxes and the squares round
        the filled button circles (the dot appears in them)."""
        out = self.boxes()
        for side in (0, 1):
            cx = self.xs[side] + _NEXT_DX[side] + _BTN_DX[side]
            out.append((cx - _BTN_R, self.y + 131 - _BTN_R, 2 * _BTN_R + 1, 2 * _BTN_R + 1))
        return out

    def draw_static(self):
        """Lettering and the empty boxes, drawn with the band by the full refresh."""
        red = display.red
        for side in (0, 1):
            x, y, sx, nx = self.xs[side], self.y, _SCORE_DX[side], _NEXT_DX[side]
            _text(red, "SCORE", x + sx + 2, y + 10, 2, 0)
            _text(red, "LINES", x + sx + 2, y + 64, 2, 0)
            _text(red, "NEXT", x + nx, y + 10, 2, 0)
            _text(red, "USER", x + nx, y + 84, 2, 0)
            _text(red, "AB"[side], x + nx + _LETTER_DX[side], y + 104, 5, 0)
            # The badge's two buttons on that side, one above the other: the menu button
            # (an outline) and the user button (filled).
            cx = x + nx + _BTN_DX[side]
            _disc(red, cx, y + 112, _BTN_R, 0)
            _disc(red, cx, y + 112, _BTN_R - 2, 1)
            _disc(red, cx, y + 131, _BTN_R, 0)
        for wx, wy, ww, wh in self.boxes():
            red.fill_rect(wx, wy, ww, wh, 0)

    def draw_values(self, score, lines, nexts, seconds, pressed=(False, False)):
        """The numbers and the next pieces: cleared and drawn again on each frame."""
        black = display.black
        for side in (0, 1):
            x, y, sx, nx = self.xs[side], self.y, _SCORE_DX[side], _NEXT_DX[side]
            black.fill_rect(x + sx, y + 28, 80, 29, 0)
            _text(black, "%04d" % min(score[side], 9999), x + sx + 4, y + 32, 3)
            black.fill_rect(x + sx, y + 82, 44, 29, 0)
            _text(black, "%02d" % min(lines[side], 99), x + sx + 4, y + 86, 3)
            black.fill_rect(x + nx, y + 28, 42, 42, 0)
            for cx, cy in nexts[side] or ():
                black.fill_rect(x + nx + 3 + cx * 9, y + 31 + cy * 9, 8, 8, 1)
            # A black dot in the middle of the filled circle while the button is down.
            bx = x + nx + _BTN_DX[side]
            black.fill_rect(bx - _BTN_R, y + 131 - _BTN_R, 2 * _BTN_R + 1, 2 * _BTN_R + 1, 0)
            if pressed[side]:
                _disc(black, bx, y + 131, _DOT_R, 1)
        black.fill_rect(self.tx, self.ty, _TIMER_W, _TIMER_H, 1)  # a black box, white digits
        _text(black, _clock(seconds), self.tx + 7, self.ty + 5, _TIMER_CELL, 0)


class _Banner:
    """A red band across the top and one across the bottom, each with its lettering
    image drawn over it."""

    def __init__(self):
        self.height = display_height * BANNER_PCT // 100
        self.foot = display_height * FOOTER_PCT // 100
        self.hud = None

    def draw(self):
        """Red band in the red plane, then the white lettering on top of it. The top band
        also gets the score boards. If an image cannot be drawn the band stays plain red."""
        if self.height:
            display.red.fill_rect(0, 0, display_width, self.height, 1)
            try:
                display.draw_png(BANNER_IMAGE, 0, 0)
            except Exception as ex:  # missing file, not a palette PNG, ...
                print("Banner image not drawn:", ex)
            if SCOREBOARD and self.height >= _HUD_H + 16:
                self.hud = _Hud(0)
                self.hud.draw_static()
        if self.foot:
            top = display_height - self.foot
            display.red.fill_rect(0, top, display_width, self.foot, 1)
            try:
                display.draw_png(FOOTER_IMAGE, top, 0)
            except Exception as ex:
                print("Footer image not drawn:", ex)


class _Plan:
    """A name cut into pieces, with the pixel layout to draw it."""

    def __init__(self, text):
        glyphs, cols = _text_cells(text)
        self.cell = max(2, min(MAX_CELL, display_width // (cols + 2)))
        self.gap = max(1, self.cell // 8)
        self.x0 = (display_width - cols * self.cell) // 2
        # The name sits BOTTOM_MARGIN pixels above the footer band, lifted by NAME_RISE_PCT;
        # y_off shifts the grid down by what is left over after whole rows.
        floor = (display_height - display_height * FOOTER_PCT // 100 - BOTTOM_MARGIN
                 - int(display_height * NAME_RISE_PCT / 100))
        self.field_rows = floor // self.cell
        self.y_off = floor - self.field_rows * self.cell
        self.y_grid = self.field_rows - GLYPH_ROWS
        # First grid row below the banner; pieces in the air stay under it.
        self.top_row = (display_height * BANNER_PCT // 100 + self.cell - 1) // self.cell
        # Row where every piece appears, and how far sideways a turned piece may reach.
        self.spawn_row = max(self.top_row,
                             (display_height * SPAWN_PCT // 100 + self.cell - 1) // self.cell)
        # Rows a piece falls in each refresh: FALL_PX worth, slowed down (in quarter rows, to no
        # less than one) until a player gets MIN_PRESS_FRAMES refreshes before the auto-turn line.
        # That line is halfway down the screen, but no lower than the last place a piece can
        # finish turning above the name (two turns, each after a fall of up to ceil(fall)
        # rows, and a piece is at most four rows tall).
        halfway = display_height * AUTO_TURN_PCT // 100 // self.cell
        fall = max(1.0, FALL_PX / self.cell)
        while True:
            whole = int(fall) + (1 if fall > int(fall) else 0)
            auto = max(self.spawn_row, min(halfway, self.y_grid - 2 * whole - 3))
            if fall <= 1.0 or (auto - self.spawn_row) / fall >= MIN_PRESS_FRAMES:
                break
            fall = max(1.0, fall - 0.25)
        self.fall_rows = fall
        self.auto_row = auto
        self.min_x = -(self.x0 // self.cell)
        self.max_x = (display_width - self.x0) // self.cell
        self.pieces = []
        self.before = []
        self.glyph_of = []  # for each piece, which letter it belongs to
        self.glyph_sizes = []  # pieces in each letter
        for cells in glyphs:
            pieces, before = _split_glyph(cells)
            base = len(self.pieces)
            self.glyph_sizes.append(len(pieces))
            for piece in pieces:
                self.glyph_of.append(len(self.glyph_sizes) - 1)
                self.pieces.append([(x, y + self.y_grid) for x, y in piece])
            for req in before:
                self.before.append(set(base + r for r in req))

    def land_sequence(self):
        """A random order the pieces could land in."""
        done = set()
        order = []
        while len(order) < len(self.pieces):
            ready = [i for i in range(len(self.pieces))
                     if i not in done and self.before[i] <= done]
            pick = random.choice(ready)
            done.add(pick)
            order.append(pick)
        return order

    def span(self, i):
        """Columns piece i covers when landed: (first, one past the last)."""
        xs = [x for x, _ in self.pieces[i]]
        return min(xs), max(xs) + 1

    def reach(self, i):
        """The most columns piece i covers in any orientation."""
        xs = [x for x, _ in self.pieces[i]]
        ys = [y for _, y in self.pieces[i]]
        return max(max(xs) - min(xs), max(ys) - min(ys)) + 1

    def lanes(self, wave):
        """The columns (first, one past the last) each piece of a wave of one or two may
        use while it is in the air, left piece first. None if they cannot be kept apart."""
        if len(wave) == 1:
            return [(self.min_x, self.max_x)]
        (a0, a1), (b0, b1) = self.span(wave[0]), self.span(wave[1])
        low = max(a1, self.min_x + self.reach(wave[0]))
        high = min(b0, self.max_x - self.reach(wave[1]))
        if low > high:
            return None
        edge = (low + high) // 2
        return [(self.min_x, edge), (edge, self.max_x)]

    def land_waves(self, at_most):
        """The pieces grouped into waves of one or two that fall together, each a list
        of piece numbers, left piece first. A wave only takes pieces whose supports have
        already landed, so none waits on another, and two share a wave only if they can
        be kept in separate columns on the way down."""
        done = set()
        waves = []
        while len(done) < len(self.pieces):
            ready = [i for i in range(len(self.pieces))
                     if i not in done and self.before[i] <= done]
            first = random.choice(ready)
            ready.remove(first)
            wave = [first]
            while at_most >= 2 and ready:
                other = random.choice(ready)
                ready.remove(other)
                pair = sorted((first, other), key=lambda i: self.span(i)[0])
                if self.span(pair[0])[1] <= self.span(pair[1])[0] and self.lanes(pair):
                    wave = pair
                    break
            done.update(wave)
            waves.append(wave)
        return waves

    def draw_cells(self, plane, cells):
        size = self.cell - self.gap
        for x, y in cells:
            plane.fill_rect(self.x0 + x * self.cell, y * self.cell + self.y_off, size, size, 1)

    def draw_hollow(self, plane, cells):
        """Blocks as a black border round white: a piece on its way down."""
        size = self.cell - self.gap
        border = 2 if size >= 8 else 1
        for x, y in cells:
            px = self.x0 + x * self.cell
            py = y * self.cell + self.y_off
            plane.fill_rect(px, py, size, size, 1)
            if size > 2 * border:
                plane.fill_rect(px + border, py + border, size - 2 * border, size - 2 * border, 0)

    def draw_piece(self, plane, cells, rows_up=0):
        size = self.cell - self.gap
        for x, y in cells:
            plane.fill_rect(self.x0 + x * self.cell,
                            (y - rows_up) * self.cell + self.y_off, size, size, 1)

    def flight_heights(self, batch):
        """How far above its landing spot each piece in the air is drawn."""
        taken = set()
        heights = {}
        for i in batch:
            top = min(y for _, y in self.pieces[i]) - self.top_row
            start = random.randint(min(FLIGHT_MIN_ROWS, top), top)
            # Nudge up to clear other pieces in the air, or down if out of room.
            up = start
            for cand in list(range(start, top + 1)) + list(range(start - 1, 0, -1)):
                if not any((x, y - cand) in taken for x, y in self.pieces[i]):
                    up = cand
                    break
            for x, y in self.pieces[i]:
                taken.add((x, y - up))
            heights[i] = up
        return heights


class TetrisTagApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        self.name = "Tetris Tag"
        self.sleep_ms = 10  # short: the time between a refresh ending and the next starting counts
        self.launched = False
        self.plans = {}
        self.word = 0
        self.state = _BUILD
        self.fast = False
        self.plan = None
        self.banner = None
        self.screen_ready = False
        self.order = []
        self.batch_size = 1
        self.frame = 0
        self.frames = 0
        self.waves = []
        self.batch = 0
        self.landed = []
        self.blink_step = 0
        self.blink_ms = 0
        self.hold_ms = 0
        self.frame_ms = 0
        self.fliers = []  # the pieces of the current wave in the air
        self.taps = [0, 0]  # presses of User A and User B since the last refresh
        self.panel = None  # the partial refresh driver
        self.task = None  # the task that owns the panel while the app runs
        self.req = None  # what it was last asked to do: _FULL or _PART
        self.pending = False  # a request is waiting or being shown
        self.failed = False  # the screen task stopped on an error
        self.beat = 0  # ticks_ms of the last run(); the task stops when it gets old
        self.film = None  # the black plane a normal refresh is about to draw
        self.need_full = True  # the next name starts with the one full refresh (red bands)
        self.wipe_step = 0
        self.score = [0, 0]  # left and right player
        self.lines = [0, 0]  # letters finished
        self.glyph_left = []  # pieces still to land in each letter
        self.last_side = 0  # which player pressed a button last
        self.seen = [False, False]  # a button was pressed since the last refresh
        self.run_start = 0  # ticks_ms when the pieces started to fall; 0 before
        self.run_end = 0  # and when the last piece landed; 0 while it runs

    def _start_word(self, idx):
        self.word = idx % len(NAMES)
        plan = self.plans.get(self.word)
        if plan is None:
            gc.collect()  # planning allocates a lot; start from a clean heap
            plan = self.plans[self.word] = _Plan(NAMES[self.word])
        self.plan = plan
        if self.banner is None:
            self.banner = _Banner()
        self.fast = (FAST and _ep is not None and not self.failed
                     and getattr(display, "_reverse_fb_bits", False))
        if self.need_full or not self.fast:
            self.screen_ready = False  # the next frame starts from a full clear and the banner
        self.frame_ms = 0
        if self.fast:
            if self.film is None:
                self.film = bytearray(len(display._framebuffer_black))
            if self.task is None or self.task.done():
                self.req = None
                self.pending = False
            self._ensure_task()
            self.waves = plan.land_waves(max(1, PIECES_AT_ONCE))
            self.order = [i for wave in self.waves for i in wave]
            self.score = [0, 0]
            self.lines = [0, 0]
            self.glyph_left = list(plan.glyph_sizes)
            self.run_start = self.run_end = 0
            self.batch = 0
            self.frame = 0
            self.landed = []
            self.state = _CLEAR
            print(f"{NAMES[self.word]}: {len(self.order)} pieces in {len(self.waves)} waves, "
                  f"cell {plan.cell}px")
            return
        self.order = plan.land_sequence()
        count = len(self.order)
        self.batch_size = max(1, (count + TARGET_FRAMES - 1) // TARGET_FRAMES)
        # One snapshot per batch in the air, then one with everything landed.
        self.frames = (count + self.batch_size - 1) // self.batch_size + 1
        self.frame = 0
        self.state = _BUILD
        print(f"{NAMES[self.word]}: {count} pieces, {self.frames} frames, cell {plan.cell}px")

    def _alive(self):
        # run() stops being called when the user leaves the app (no hook for that), so
        # the task gives the panel back when the calls stop. Allow for the PNG decode and
        # a garbage collection, a second or so each, on top of a frame's work.
        return time.ticks_diff(time.ticks_ms(), self.beat) < _ALIVE_MS

    async def _screen(self):
        """Owns the panel while the app runs: does the normal and partial refreshes that
        run() asks for, one at a time, and hands the panel back when the app stops."""
        if self.panel is None:
            self.panel = _Panel()
        panel = self.panel
        session = False
        asleep = False
        still = time.ticks_ms()
        try:
            while self._alive():
                req = self.req
                if req is None:
                    if session and not asleep and time.ticks_diff(time.ticks_ms(), still) > _IDLE_MS:
                        await panel.sleep()  # the picture sits still: no point keeping the boost up
                        asleep = True
                    await aio.sleep_ms(5)
                    continue
                self.req = None
                if req == _FULL:
                    if session:
                        await panel.stop()
                        session = False
                    display.update()
                    while not display.is_done_updating():
                        await aio.sleep_ms(50)
                else:
                    if not session:
                        await panel.start_keep(self.film)
                        session = True
                    memoryview(panel.buf)[:] = display._framebuffer_black
                    await panel.show()
                    asleep = False
                still = time.ticks_ms()
                self.pending = False
        except Exception as ex:
            print("Tetris Tag: screen stopped:", ex)
            sys.print_exception(ex)
            self.failed = True
            self.pending = False
        finally:
            if session:
                try:
                    await panel.stop()
                except Exception as ex:
                    print("Tetris Tag: panel stop:", ex)

    def _ensure_task(self):
        """Make sure the screen task is running (it stops when run() goes quiet)."""
        if not self.failed and (self.task is None or self.task.done()):
            self.beat = time.ticks_ms()
            self.task = aio.create_task(self._screen())

    def _request(self, kind):
        """Ask the screen task to show what is in the framebuffers."""
        self._ensure_task()
        if kind == _FULL:
            memoryview(self.film)[:] = display._framebuffer_black  # what the film will show
        self.req = kind
        self.pending = True
        self.frame_ms = time.ticks_ms()

    def _full(self):
        """A normal refresh of the whole panel (about 35 s)."""
        if self.fast:
            self._request(_FULL)
        else:
            display.update()
            self.frame_ms = time.ticks_ms()

    def _new_frame(self):
        """Blank canvas for the next frame. The banner is drawn once per name and then
        left where it is: redrawing it would decode the image for every frame."""
        if not self.screen_ready:
            display.clear()
            self.banner.draw()
            self._draw_hud()
            self.screen_ready = True
            return
        top = self.banner.height
        rows = display_height - self.banner.foot - top  # between the two bands
        display.black.fill_rect(0, top, display_width, rows, 0)
        display.red.fill_rect(0, top, display_width, rows, 0)

    def _show(self, landed, in_air=()):
        """Slideshow: queue a snapshot, landed pieces in black, in the air in red."""
        plan = self.plan
        self._new_frame()
        for i in landed:
            plan.draw_piece(display.black, plan.pieces[i])
        heights = plan.flight_heights(in_air)
        for i in in_air:
            plan.draw_piece(display.red, plan.pieces[i], heights[i])
        display.update()
        self.frame_ms = time.ticks_ms()

    def _show_frame(self):
        landed = self.order[:self.frame * self.batch_size]
        in_air = self.order[len(landed):len(landed) + self.batch_size]
        self._show(landed, in_air)
        self.frame += 1

    def _draw_hud(self):
        """The numbers and next pieces on the score boards (if there are any)."""
        hud = self.banner.hud if self.banner else None
        if hud is None:
            return
        seconds = 0
        if self.run_start:
            seconds = time.ticks_diff(self.run_end or time.ticks_ms(), self.run_start) // 1000
        waves = self.waves if self.fast else ()
        upcoming = waves[self.batch + 1] if self.batch + 1 < len(waves) else ()
        nexts = []
        for side in (0, 1):
            if side < len(upcoming):
                cells = self.plan.pieces[upcoming[side]]
                left = min(x for x, _ in cells)
                top = min(y for _, y in cells)
                nexts.append([(x - left, y - top) for x, y in cells])
            else:
                nexts.append(None)
        pressed = (self.seen[0] or bool(getattr(UserA, "value", False)),
                   self.seen[1] or bool(getattr(UserB, "value", False)))
        hud.draw_values(self.score, self.lines, nexts, seconds, pressed)

    def _credit(self, flier, side):
        """A piece has landed: points to the player at `side`, and a line if it finished
        its letter."""
        points = 10 + (10 if flier.touched and not flier.auto else 0)
        letter = self.plan.glyph_of[flier.i]
        self.glyph_left[letter] -= 1
        if self.glyph_left[letter] == 0:
            self.lines[side] += 1
            points += 100
        self.score[side] += points

    def _show_fast(self, moving, settled=()):
        """Partial refresh: landed pieces in solid black, the ones in the air as a black
        border round white (`moving`, a list of cell lists), and the ones that have just
        dropped into place solid black (`settled`)."""
        plan = self.plan
        self._new_frame()
        for i in self.landed:
            plan.draw_piece(display.black, plan.pieces[i])
        for cells in settled:
            plan.draw_cells(display.black, cells)
        for cells in moving:
            plan.draw_hollow(display.black, cells)
        self._draw_hud()
        self._request(_PART)

    def _drop_frame(self):
        """One partial refresh of the current wave. The first one shows the pieces at
        the spawn height, turned a random way. After that each piece that is not yet the
        right way up falls a few rows, turned by its player if they pressed a button,
        or by itself once it is low enough. A piece that is the right way up drops into
        place, still an outline, and turns solid black one refresh later; the wave is over
        when every piece in it has."""
        wave = self.waves[self.batch]
        self.seen = [self.taps[0] > 0, self.taps[1] > 0]  # shown on the buttons drawn at the score boards
        if not self.run_start:
            self.run_start = time.ticks_ms() or 1  # the clock starts with the first piece
        if self.frame == 0:
            lanes = self.plan.lanes(wave)
            self.fliers = [_Flier(self.plan, i, lanes[n], n) for n, i in enumerate(wave)]
            self.taps = [0, 0]
        else:
            auto_row = self.plan.auto_row
            for f in self.fliers:
                if len(self.fliers) == 1:
                    turns = self.taps[0] + self.taps[1]
                else:
                    turns = self.taps[f.side]
                was = f.landed
                f.step(turns % 4, auto_row)
                if f.landed and not was:
                    self._credit(f, f.side if len(self.fliers) > 1 else self.last_side)
            self.taps = [0, 0]
        self._show_fast([f.cells for f in self.fliers if not f.landed],
                        [f.cells for f in self.fliers if f.landed])
        if all(f.landed for f in self.fliers):
            self.landed.extend(wave)
            self.batch += 1
            self.frame = 0
        else:
            self.frame += 1

    def _start_blink(self):
        self.state = _BLINK
        self.run_end = time.ticks_ms() or 1  # the timer stops
        self.blink_step = 0
        self.blink_ms = time.ticks_ms()
        if BLINK_MODE == "leds":
            leds.fill(BLINK_COLOR)

    def _start_hold(self):
        """The name stays up, untouched, for HOLD_S seconds."""
        self.state = _HOLD
        self.hold_ms = time.ticks_ms()
        print(f"Holding {NAMES[self.word]} for {HOLD_S} s")

    def _wipe(self, then):
        """Clear the area between the two red bands with partial refreshes instead of a full
        one: all black for a refresh, then all white, which also takes the ghosts off. The
        bands are not driven, so they stay as they are."""
        top = self.banner.height
        rows = display_height - self.banner.foot - top
        if self.wipe_step == 0:
            display.black.fill_rect(0, top, display_width, rows, 1)
            self.wipe_step = 1
        else:
            self._new_frame()  # white between the bands
            self.wipe_step = 0
            self.state = then
        self._request(_PART)

    def _blink_leds(self):
        if time.ticks_diff(time.ticks_ms(), self.blink_ms) < BLINK_MS:
            return
        self.blink_ms = time.ticks_add(self.blink_ms, BLINK_MS)
        self.blink_step += 1
        if self.blink_step >= 2 * BLINK_COUNT:
            leds.fill((0, 0, 0))
            self._start_hold()
        else:
            leds.fill(BLINK_COLOR if self.blink_step % 2 == 0 else (0, 0, 0))

    def _blink_screen(self):
        # The name is up. Alternate blank and name, once per finished refresh.
        if self.blink_step >= 2 * BLINK_COUNT:
            self._start_hold()
            return
        shown = self.order if self.blink_step % 2 else []
        if self.fast:
            self.landed = shown
            self._show_fast([])
        else:
            self._show(shown)
        self.blink_step += 1

    def run(self):
        try:
            self._run()
        finally:
            # The task resumes when run() returns, so this is the heartbeat it checks. A
            # long run() (planning, a PNG decode) must not look like the app having stopped.
            self.beat = time.ticks_ms()

    def _run(self):
        self.beat = time.ticks_ms()
        if self.launched:
            leds.fill((0, 0, 0))
            self.failed = False
            self.need_full = True  # whatever was on the panel, draw the red bands afresh
            self._start_word(0)
            return

        if self.state == _DROP and self.fast:
            # Presses wait for the next refresh, which turns the piece.
            if UserA.pressed():
                self.taps[0] += 1
                self.last_side = 0
                print("User A: turn left piece")
            if UserB.pressed():
                self.taps[1] += 1
                self.last_side = 1
                print("User B: turn right piece")
        elif UserA.pressed():
            leds.fill((0, 0, 0))
            self._start_word(self.word + 1)
        elif UserB.pressed():
            leds.fill((0, 0, 0))
            self._start_word(self.word)

        # A refresh in progress owns the framebuffers.
        if self.pending or not display.is_done_updating():
            return

        if self.frame_ms:
            print("refresh ms:", time.ticks_diff(time.ticks_ms(), self.frame_ms))
            self.frame_ms = 0

        if self.state == _CLEAR:
            if self.need_full:
                # The only full refresh, when the app starts: it draws the red bands,
                # which the partial refreshes then leave alone.
                self.screen_ready = False
                self._new_frame()
                self._full()
                self.need_full = False
                self.state = _DROP
            else:
                self._wipe(_DROP)  # a new name: wipe what is between the bands
        elif self.state == _DROP:
            if self.failed:
                # The partial refresh stopped working: the slideshow still works.
                print("Partial refresh stopped working, using the slideshow")
                self._start_word(self.word)
            elif self.batch < len(self.waves):
                self._drop_frame()
            else:
                self._start_blink()
        elif self.state == _BUILD:
            if self.frame < self.frames:
                self._show_frame()
            else:
                self._start_blink()
        elif self.state == _HOLD:
            if time.ticks_diff(time.ticks_ms(), self.hold_ms) >= HOLD_S * 1000:
                self._start_word(self.word + 1)
        elif BLINK_MODE == "leds":
            self._blink_leds()
        else:
            self._blink_screen()
