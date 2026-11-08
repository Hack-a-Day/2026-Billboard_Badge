# Fast refresh on the Supercon 2026 "Billboard" badge

How the 9.7" three-colour e-paper on the badge is driven at about 0.75 frames a second instead of
one frame every 34 seconds. Everything here was measured on one badge (Joel's) during Supercon
2026 with MicroPython 1.24.1; nothing came from a datasheet. Code: `firmware/badge/lib/epd.py` (driver,
based on the repo's PR #3 by FacioErgoSum; the waveform builders `anim_lut()` and `trans_lut()`
are at the bottom of it) and `firmware/badge/apps/fastdemo.py` (a minimal frame loop).

## 1. The hardware, as far as it matters

| | |
|---|---|
| Panel | Pervasive Displays E2969JS0B1, 9.7", 960 x 672, black / white / red |
| Controller | two chip-on-glass driver ICs ("driver B" family), one per half of the panel, each on its own chip select and BUSY line, sharing SPI |
| MCU | ESP32-S3, MicroPython 1.24.1 + LVGL, hardware SPI at 8 MHz |
| Pins | SCL 21, SDA 47, DC 10, RST 11, CS1 13, CS2 3, BUSY1 12, BUSY2 8 |
| Image memory | two bit planes per half: command 0x10 and command 0x11, 40,320 bytes each (480 x 672 / 8). In the stock driver 0x10 is black and 0x11 is red |
| Stock full refresh | 34 s on this badge (the repo author saw 17 s). Waveform comes from the panel's OTP |
| Stock "fast" refresh (PR #3) | 6.5 s, three pulls of a built-in table, black and white only |

## 2. What the waveform register looks like

The controller accepts a custom waveform with command `0xB0` followed by **400 bytes**:

```
5 tables x 10 groups x 8 bytes
table   = which pixel state this applies to (see section 3)
group   = one step of the waveform
8 bytes = [level, f0, f1, repA, f2, f3, repB, repAll]
```

- `level` is four 2-bit fields, one per phase of the group. Phase values: `0` ground (no drive),
  `1` VDH (drive toward ink / black), `2` VDL (drive toward paper / white), `3` VDHR (red high,
  unused here). So `0x55` = ink in all four phases, `0xAA` = paper in all four, `0x99` =
  paper-ink-paper-ink (the "shake"), `0x00` = idle.
- `f0..f3` are the frame counts for the four phases, `repA/repB/repAll` repeat counts. We leave
  them all at 1.
- Table 4 (the fifth) is VCOM; we leave it at ground.

Measured facts that constrain everything:

| Fact | Value |
|---|---|
| One waveform frame | 29 ms |
| Minimum per group | 4 frames (counts of 1 still cost 4 frames); all 10 groups always run |
| Hard floor per refresh | 10 x 4 x 29 ms = **1160 ms** |
| A count of 0 | runs forever (BUSY never releases) |
| OSC register 0x03 | no effect on frame time |
| Partial window | none; every refresh is the full panel |

So the refresh time is fixed at about 1.16 s whatever the table says; the only design freedom is
what each group does to each class of pixel.

## 3. How pixels pick their table

The controller forms a 2-bit index from the two image planes for every pixel:

```
table = (bit from plane 0x11) << 1 | (bit from plane 0x10)
```

Three ways to use that:

1. **Plain black and white** (`anim_lut()`): plane 0x11 all zero, so only tables 0 (white) and 1
   (black) are used. Table 0 drives paper, table 1 drives ink. Simple, and every pixel gets
   driven every frame, which keeps ghosts down.
2. **Transition tables** (`trans_lut()`, current default for the animation apps): send the
   *previous* frame in plane 0x11. Then table 0 = stayed white, 1 = went black, 2 = went white,
   3 = stayed black. Changed pixels get a shake group then the drive; unchanged pixels get the
   drive in their own direction with no shake. Pushing black blacker is invisible but still
   clears residue. (A version that left unchanged pixels idle ghosted badly within minutes.)
3. **Four greys** (`gray_lut()`, in a picture-viewer app that is not part of the public tree; the
   table is three lines of the same builder): after an erase-to-white, table 1 (black
   bit) gets the full ink drive, table 2 (red bit only) gets one group of ink = light grey, table 3
   (both bits) gets three groups = dark grey. The red plane becomes a grey-level plane. Only good
   for stills; every frame starts from white.

Red itself cannot be done this way: the fast pulls leave red pale and it fades with each frame.
Red content uses the stock 34 s refresh.

## 4. The waveform we settled on

`anim_lut(drive_groups=7, shake=True, preclear=0)`, chosen by eye from an A/B test of six
variants, is the baseline ("variant 4"):

```
group 1      : 0x99  shake  (paper, ink, paper, ink)
groups 2..8  : drive (0xAA paper for a white pixel, 0x55 ink for a black one)
groups 9..10 : idle
```

`trans_lut(7, True)` is the same drive with the shake only on tables 1 and 2 (pixels that
changed). This is what the animation apps run.

Stills (a picture viewer, a help page) use `anim_lut(6, True, 2)`: two groups of paper first (erase to
white, visible as a flash), then shake, then six of drive. Best ghost suppression, one flash per
picture.

## 5. A refresh, step by step

Opening a session (`display.anim_begin(lut)`):

1. Wait for any stock refresh to finish (`wait_done`), then hardware reset (RST low 20 ms).
2. Send both planes for both halves (section 6).
3. `cog_initial()`: the stock power-on sequence (0x05, 0xD8, 0xD6, 0xA7 pulses, 0x03 OSC,
   0x44/0x45 temperature = 0x82 for 25 C, per half).
4. `0xB0` + the 400-byte table.
5. DC/DC soft start (`0x51` / `0x09` ramp, same as the stock driver).
6. Trigger: `0x15 0x3C`.

Every further frame (`display.anim_frame()`):

1. Split the 960-wide framebuffer into left and right halves, bit order reversed for driver B.
2. Poll BUSY (both halves) every 2 ms until low.
3. Send both planes for both halves (about 190 ms, see section 6).
4. Trigger `0x15 0x3C`. Return; the refresh (1160 ms) runs while the app renders the next frame.

The DC/DC stays on between frames. Closing (`anim_end`): wait for BUSY, DC/DC soft shutdown.
Do a stock `update()` before any red content or after a long session (the apps do one every ten
minutes and on exit).

Frame period = 1160 ms refresh + ~190 ms send + a few ms = **about 1.35 s**, as long as the app
renders the next frame in under ~1.1 s. Rendering happens during BUSY, so it is free up to that.

## 6. Sending the image data

Per frame, for each half (CS1 left, CS2 right):

```
0x13  00 3B 00 00 9F 02      DUW   (window, both halves, sent once)
0x90  00 3B 00 A9            DRFW  (sent once)
0x12  3B 00 14               RAM_RW, to one half
0x10  <40320 bytes>          plane A (black / "new")
0x12  3B 00 14
0x11  <40320 bytes>          plane B (red / "old" / grey)
```

Both planes must be sent every frame. Skipping plane 0x11 to save time made the controller use
stale data and ghosted badly. The SPI write is done in 20 KB chunks with an asyncio yield between
them: 4 KB chunks with a yield each cost 280 to 780 ms because other tasks ran in the gaps;
20 KB chunks send the four planes in ~190 ms at 8 MHz. 10 MHz would save ~35 ms and 20 MHz
~110 ms, but the panel's rated SPI maximum was never confirmed so the clock stays at 8 MHz.

## 7. Keeping the app inside the 1160 ms window

Measured on the badge:

| Per frame | Cost | What we did |
|---|---|---|
| A blank LVGL screen pass (create, load, `refr_now`) | 572 ms + ~100 KB garbage | Removed. All scenes draw straight into the framebuffer with `@micropython.viper` helpers |
| `gc.collect()` on the ~8 MB PSRAM heap | 658 ms | Only at scene changes, or if free memory drops under 1 MB (viper scenes make ~2 KB of garbage a frame) |
| Typical scene render | 75 to 450 ms | Fits inside BUSY |

The framebuffer is 1 bit per pixel, 120 bytes per row, MSB first, bit set = ink. Drawing in viper:
`fb[y * 120 + (x >> 3)] |= 0x80 >> (x & 7)`. Scenes that want white-on-black draw normally and
invert the plane at the end.

## 8. Ghosting

What is left after a fast frame is residue from pixels that were not driven hard enough. Levers,
in order of usefulness:

1. Drive every pixel every frame (section 3, options 1 and 2 with maintenance drive).
2. A wipe frame between scenes in the background colour of the next scene: invisible, but it
   drives every pixel the previous foreground occupied back to background.
3. Erase-to-white groups (`preclear`) for stills, at the cost of a flash.
4. A stock full refresh every `FULL_CLEAN_EVERY_S` (600 s).

Things that did not help: fewer drive groups (less ghost suppression), OSC register, skipping
plane 0x11, two-frame black + white wipes (slow and visible).

## 9. Gotchas on this badge

- The stock driver task parks in an `asyncio.Event`; never start a second `display.run()`
  from a test script, the two fight over the panel.
- `mpremote` must be used with `resume`; a soft reset crashes LVGL re-init. Hard reset with
  `machine.reset()`. Never call `machine.bootloader()` over the TinyUSB port (it wedges the USB
  device).
- Waveform tables are latched at the trigger. Writing a new `0xB0` mid-session between frames
  is how the picture viewer switches between stills, animation and grey tables (the driver re-opens the
  session: `anim_end` + `anim_begin`, ~2.5 s).
- Temperature is fixed at 25 C in the init sequence. The panel was only ever tested indoors.

## 10. Minimal example

`firmware/badge/apps/fastdemo.py` is the whole thing in one small app. The core of it:

```python
import framebuf
from lib.epd import anim_lut, display, display_width, display_height

plane = display._framebuffer_black           # 960 x 672, 1 bit, MSB first, 1 = black
fb = framebuf.FrameBuffer(plane, display_width, display_height, framebuf.MONO_HLSB)

await display.wait_done()                    # let any stock refresh finish
fb.fill(0)
await display.anim_begin(anim_lut())         # power up, load the waveform, show the (white) frame
for n in range(40):
    fb.fill(0)
    fb.fill_rect(100 + n * 15, 200, 120, 120, 1)
    await display.anim_frame()               # ~1.35 s; render the next frame while it runs
await display.anim_end()
display.clear()
display.update()                             # stock full refresh when you are done
```

For `trans_lut()` keep a copy of each frame and put it in `display._framebuffer_red` before
the next `anim_frame()`; the example app's comments show where.

*Measured 2026-11-06 to 2026-11-08 at Supercon 2026.*
