# Tetris Tag

Tetris Tag takes inspiration from Tetris to assemble your name on the badge. Use User A and B buttons to play!

Tetris-like pieces fall and stack up to spell a name along the bottom edge of
the screen, two at a time, and you steer them. The name blinks three times, stays up for
30 seconds, then it is built again, and it loops forever. It ships with the placeholder
"YOUR NAME"; put your own in `NAMES` (see below).

Author: Nikita Pashenkov, <https://github.com/pa-nik>

Concept by the Soviet people, execution by [pa-nik](https://github.com/pa-nik) and Claude.

Controls:
```
User A: turn the left falling piece 90 degrees clockwise (bottom left button)
User B: turn the right falling piece 90 degrees clockwise (bottom right button)
Menu +/-: back to the app menu (as for every app)
```
The two top buttons are Menu -/+, which the badge firmware reserves for leaving an app, so
the game uses the two bottom ones (positions from the PCB layout in `hardware/billboard_pcb`).
Between names (while the screen is clearing, the name blinks or the finished name is held)
User A skips to the next name and User B restarts the current one.

To install, run this from the root of the repo (the app also needs the newer display driver
and its two banner images; see Installing below):
```
mpremote cp firmware/badge/lib/epd.py :lib/epd.py
mpremote cp firmware/badge/lib/epd_partial.py :lib/epd_partial.py
mpremote cp firmware/badge/lib/large_png.py :lib/large_png.py
mpremote cp firmware/badge/data/rendered_luts.py :data/rendered_luts.py
mpremote cp user_apps/tetris_tag/tetris_banner.png :data/graphics/tetris_banner.png
mpremote cp user_apps/tetris_tag/tetris_footer.png :data/graphics/tetris_footer.png
mpremote cp user_apps/tetris_tag/tetris_tag.py :apps/tetris_tag.py
mpremote reset
```
Then select "Tetris Tag" from the menu.

## How to play

Two pieces appear together at 25% of the screen height, each turned a random quarter
turn. They fall a few rows per refresh (about 0.65 s) **without turning** until you turn
them. Press User A for the piece on the left and User B for the one on the right; each
press turns it 90 degrees clockwise at the next refresh. When a piece is the right way up
it drops into its place on the next refresh. A piece you leave alone turns itself,
90 degrees a refresh by the shorter way, once it reaches 50% of the screen height (a little higher, see Footer)
(`AUTO_TURN_PCT`), so the game plays itself if nobody presses. Buttons only count above
that line. Each piece stays in its own half of the screen while it is falling, so the two
never collide. When one piece is alone (the last of a letter), either button turns it.

## Make it your own

Edit the top of `tetris_tag.py`:

- `NAMES`: what to show, in order. It starts as `("YOUR NAME",)`: replace that with your
  own name, or list several to cycle through them. Only characters in `FONT` can be
  shown: the letters A to Z and a to z, é, and space. There are no digits or punctuation yet:
  add a glyph to `FONT` for any other character (5 wide, 7 tall, `X` is a block). Long names
  get small blocks and take longer, since the pieces fall a fixed distance per refresh.
- `PIECES_AT_ONCE`, `SPAWN_PCT`, `FALL_PX`, `AUTO_TURN_PCT`: how many pieces fall at a time
  (1 or 2), the screen height they start from, how far they fall each refresh in pixels
  (36: 3 rows for 12 px blocks, 2 rows for 16 px blocks; larger is a faster game but leaves
  less time to press a button before the piece turns itself), and the screen height where an
  unturned piece starts turning itself.
- `BLINK_MODE`, `BLINK_COUNT`, `BLINK_MS`: see below. `HOLD_S`: how long the finished name
  stays up after the blinking (30 s).
- `BANNER_PCT`, `BANNER_IMAGE`: the banner height and its image (see Banner).
- `FOOTER_PCT`, `FOOTER_IMAGE`, `BOTTOM_MARGIN`: the footer band at the bottom, its image, and the
  gap between the name and the footer (see Footer).

## Footer

A second red band runs across the bottom 25% of the screen, and the name lands right above it
(`BOTTOM_MARGIN`, 8 px). It is meant for your web address or social handles; it ships with the
placeholder "your web/@socials". Make your own the same way as the banner, on your computer:

```
python3 make_banner.py --text "yourname.com" --pct 25 --out tetris_footer.png
mpremote cp tetris_footer.png :data/graphics/tetris_footer.png
```

(`--text "@you"` for a handle; the same Helvetica Bold as the banner.) If the image is not on the
badge the footer is a plain red band; `FOOTER_PCT = 0` removes the footer.

With the banner on top and the footer at the bottom the pieces have a play field of about 336 px
between them, so they fall a fixed distance per refresh (`FALL_PX`) and the line where an
unturned piece starts turning itself (`AUTO_TURN_PCT`, 50% of the screen height) is moved up as
far as it must be for the piece to finish turning above the name: about 40% of the screen height
for 16 px blocks, about 45% for 12 px blocks.

## Banner

A red banner runs across the top 25% of the screen, with "ТЕТРИС ☭ TAG" in white
Helvetica Bold centred on it. Pieces fall in the space below it.

The lettering is an image, `tetris_banner.png`, not text drawn by the badge: the badge
has no Helvetica, and its built-in font has neither Cyrillic nor the hammer and sickle.
`make_banner.py` makes it on your computer:

```
pip install pillow fonttools
python3 make_banner.py                                   # rewrites tetris_banner.png
python3 make_banner.py --text "HELLO" --pct 20           # other text or height
mpremote cp tetris_banner.png :data/graphics/tetris_banner.png
```

It sets the text in the Helvetica Bold that ships with macOS, which has the Cyrillic
and Latin letters. Helvetica has no hammer and sickle, so that one glyph comes from a
fallback font (Apple Symbols by default, thickened a little to match the bold letters;
`--symbol-font` picks another). Only the rendered shapes end up in the PNG, no font file.
`BANNER_PCT` in `tetris_tag.py` and `--pct` must agree (25% is 168 px). If the image
is not on the badge, the banner is a plain red band.

The panel can only make red with a full refresh, so the banner and the footer are drawn once, by
the one full refresh when the app is launched (about 36 s, the first LED blinks red), and the partial
refreshes then leave it untouched.

## Partial refresh

The panel's own refresh takes about 30 s, which is no use for animation. This app uses the
partial refresh driver `firmware/badge/lib/epd_partial.py` (`PartialEPD`), which redraws only
the pixels that changed, in about 0.65 s a frame (measured on a badge: median 629 ms):

1. When the app is launched there is one normal full refresh to a blank field with the red
   bands (about 36 s). Each new name after that starts with a partial wipe of the field
   instead: all black for one refresh, then all white (about 1.5 s), which also takes most
   ghosting off. The app opens its partial session on top of what the full refresh drew
   (`PartialEPD.start()` would flash the panel white first, which would also wipe the red
   bands, so the app has its own `start_keep`). The partial pulls only touch the pixels around
   what changed, so the bands are not driven.
2. The pieces then fall two at a time (see How to play). A fall is one refresh to show the
   pieces, a few more while they fall and turn, and one to drop each into place. A square,
   which looks the same turned, is never turned and drops straight away.
   Pieces are two to four blocks, with no single-block pieces. A piece normally joins blocks
   edge to edge, like a real Tetris piece, but where a letter needs it (the diagonal strokes
   of K, N, R, V, Y) a piece may join two blocks that touch only at a corner, a shape the real
   game does not have. The splitter prefers real shapes and fewer pieces. A block with nothing
   around it at all, such as the dot of a lowercase i, would still be alone; none of the
   built-in letters in the default name do that.
3. A background task owns the panel while the app runs, as in the Pong app: it hands the panel
   back about 2.5 s after you leave the app and powers it down after 2 s without changes.
   The LEDs only blink at the end of a name.

Ghosting: partial refreshes leave light ghosts that build up; the black-then-white wipe at the
start of each name takes most of them off, and a full refresh (relaunch the app) takes off all.

Red pieces were tried (falling red, then one extra refresh to turn black) and did not work
on a partial refresh: it is black and white by design and cannot make a good red on this
panel. The red bands are unaffected: they are not driven.

Speed. Each refresh takes about 0.65 s, which is the driver's floor, so the length of a pass
depends on how many refreshes it needs. A pass of "YOUR NAME" is 37 pieces in about 19 waves
and, in simulation, about 100 refreshes if nobody presses a button (about 65 s) and fewer if you
turn the pieces: a piece you turn right way up early drops sooner. The app prints `refresh ms:`
over serial after every frame, so you can see the real times.

If the badge is not a B1 panel, or the firmware does not have `epd_partial.py`, or the partial
refresh stops working, the app falls back to a slideshow of full refreshes, one per snapshot,
with the pieces in the air in red. The slideshow does not turn the pieces.

## Blinking and holding

The paper cannot refresh in half a second, so `BLINK_MODE = "leds"` (the default)
keeps the finished name on the paper and blinks the LED bar, half a second on and
half a second off, three times. `BLINK_MODE = "screen"` blinks the paper itself
(name, blank, name...), but each step is at least one drive step, so it is much slower.

When the blinking is over the finished name stays on the screen, untouched, for `HOLD_S`
seconds (30) before the next round starts with a partial wipe. User A skips the wait to
the next name and User B restarts the current one right away.

## Installing

The commands at the top copy the app, its two banner images and the display files it needs.
The app needs the newer display driver on the badge as well as the app itself. Badges that
shipped with older firmware are missing `rendered_luts.py`, which the partial refresh uses, and
an updated `epd.py` (it has the display lock) and `large_png.py`, which `epd_partial.py` relies on. `mpremote ls :lib :data` shows
what is on the badge. After the first install only `tetris_tag.py` changes, so
`mpremote cp user_apps/tetris_tag/tetris_tag.py :apps/tetris_tag.py` and a reset is enough.

Alternatively, `python scripts/update.py push` from `firmware/` brings the whole badge up
to date, but it deletes anything on the badge that is not in `firmware/badge/`,
including this app, so copy the app again afterwards.
