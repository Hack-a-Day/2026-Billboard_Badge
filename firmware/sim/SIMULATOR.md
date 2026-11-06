# Badge Simulator

Runs the Supercon 2026 Billboard Badge firmware (`firmware/badge/`) on a laptop. The window shows the e-paper panel, the 8 RGB LEDs and the status LED, and the buttons. The buttons respond to the keyboard or the mouse. SAOs can be emulated by programs that connect over TCP.

- `sim.py` is the whole simulator: one MicroPython file, run by lvgl_micropython's Unix port.
- `badge-sim` is a launcher that runs `sim.py` with the right interpreter and heap size.
- `sao_example.py` is an example SAO (a small I2C memory) to copy.
- `frozen_manifest.py` is a build setting for the interpreter (see [Build](#build)).

The firmware runs as it is; only the hardware edges are replaced.

## Quick start

Build the interpreter once (see [Build](#build)), then from the repo root:

```bash
firmware/sim/badge-sim                    # or run it from anywhere
firmware/sim/badge-sim --scale 0.7        # smaller window
firmware/sim/badge-sim --real-refresh     # e-paper takes as long as the real one
python3 firmware/sim/sao_example.py       # in another terminal: an SAO at I2C address 0x50
```

| Option | Meaning |
|---|---|
| `--scale F` | Initial window scale (default 1.0, so the panel is 1:1, about 1008×880). The window can be resized freely; the badge keeps its shape, with black around it. |
| `--real-refresh` | The image appears when the panel would finish refreshing, not as soon as the firmware calls `display.update()`. |
| `--bw-time S` / `--bwr-time S` | Refresh time for an image without / with red (default 2 and 20 seconds). |
| `--led-full N` | LED channel value drawn at full brightness (default 4). See [LEDs](#leds). |
| `--sao-port P` | TCP port for SAO emulators (default 47100). `0` turns the SAO bus off. |

Set `LVGL_MICROPY` to point `badge-sim` at an interpreter somewhere other than `firmware/sim/lvgl_micropython/build/lvgl_micropy_unix`.

The firmware's writable state lives in `firmware/sim/data/` (git-ignored), which is copied from `firmware/badge/data/` on the first run (missing files are copied on every run). Delete `firmware/sim/data/` to "factory reset" the simulated badge. `firmware/badge/data/` is never written to.

### Buttons

| Key | Button |
|---|---|
| A | User A |
| Left | Menu - |
| Right | Menu + |
| B | User B |
| Space | Boot |

The on-screen buttons can also be clicked and held. The screen buttons are labeled with the firmware's names and laid out in the badge's switch order (SW4–SW7, then Boot). `lib/button.py` notes that the silkscreen swaps + and -. Boot (GPIO0) is the strapping pin only at reset; at runtime it's an ordinary button that `lib/button.py` scans and apps can read.

### LEDs

The firmware uses small channel values like `(0,1,1)` and `(3,2,1)` because the real LEDs are very bright. Drawn as-is they would look black, so each channel is drawn at `sqrt(value / led-full)` of full brightness. With the default of 4, 1 shows at half brightness and 4 or more at full. An LED with all channels at 0 is drawn as a dark gray block.

As on the badge, the window only changes when the firmware calls `write()` on the NeoPixel. Setting `leds[i]` without writing shows nothing on the badge, and nothing in the sim. Values that don't fit in a byte raise the same `ValueError` the real `neopixel` module does.

The round "status" indicator is the status LED on GPIO1 (`lib/board.py`). It's drawn green; the real LED's color may differ.

## Build

The sim runs on [lvgl_micropython](https://github.com/lvgl-micropython/lvgl_micropython) built for Unix with SDL. Note that this is **not** the lv_micropython the badge itself is now built with; see [Known gaps](#known-gaps). The build was verified on Ubuntu with lvgl_micropython `d2d2646` (June 2026), which builds MicroPython 1.27.0.

```bash
# Ubuntu dependencies (from the lvgl_micropython README; SDL2 is compiled by the build).
# Keep this on one line when pasting.
sudo apt-get install -y build-essential libffi-dev pkg-config cmake ninja-build libasound2-dev libpulse-dev libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxfixes-dev libxi-dev libxss-dev libxkbcommon-dev libdrm-dev libgbm-dev libgl1-mesa-dev libgles2-mesa-dev libegl1-mesa-dev libdbus-1-dev libibus-1.0-dev libudev-dev libwayland-dev libdecor-0-dev

cd firmware/sim
git clone https://github.com/lvgl-micropython/lvgl_micropython   # git-ignored
cd lvgl_micropython

# All Montserrat font sizes (the apps use 14, 16, 24 and 48; turning them all on
# means new apps don't need a rebuild)
sed -i -E 's/^#define LV_FONT_MONTSERRAT_([0-9]+) +0/#define LV_FONT_MONTSERRAT_\1 1/' lib/lv_conf.h

# About 10 minutes the first time; it downloads MicroPython and SDL2.
# FROZEN_MANIFEST puts asyncio back in (lvgl_micropython's Unix build leaves it out).
python3 make.py unix DISPLAY=sdl_display INDEV=sdl_pointer FROZEN_MANIFEST=../frozen_manifest.py
chmod +x build/lvgl_micropy_unix   # the build says to use sudo; it isn't needed
```

The interpreter segfaults on every exit, even after `-c 'print(1)'`. That's a bug in lvgl_micropython's Unix shutdown code and it's harmless.

## Firmware changes

These are bug fixes that apply to the real badge too. Without them, the sim couldn't run the firmware unmodified:

| File | Change | Why |
|---|---|---|
| `lib/led_bar.py` | Chooses the real LEDs by whether `neopixel` can be imported, not by `sys.platform == "esp32"`. The no-LED fallback is now a list with `fill()` and `write()`. | The fallback's `fill()` and `clear()` crashed (a plain list has neither). Keying on `neopixel` also lets the sim supply a fake `neopixel` module and exercise the badge's own code path. |
| `lib/app_switcher.py` | Reads `entry[0]` from `os.ilistdir()` instead of unpacking 4 values. | MicroPython documents the entries as 3- *or* 4-tuples. The badge's filesystem gives 4; the host gives 3. |
| `lib/app_switcher.py` | Menu order comes from an explicit `APP_ORDER` list. Unlisted apps come after the listed ones, sorted by name. | The order came from iterating module dictionaries, which follows hash-table placement. It was the same on every boot of one badge, but it differed between the badge and the sim, and adding an app could shuffle the others. |
| `lib/sao.py` | Removed the `/` (positional-only marker) from `Sao.readfrom`. | MicroPython doesn't support that syntax, so `lib/sao.py` couldn't be imported on the badge either. |
| `main.py` | `never-epd.png` → `never_epd.png`. | The file's real name; the 42nd boot would have crashed. |
| `apps/nametag.py` | `display.lv_display.inv_p = 0` moved into `_discard_pending_redraws()`, which falls back to rendering the pending areas into a flush that discards them. | `inv_p` is a private LVGL field. lvgl_micropython's bindings don't expose it, and LVGL's public way to clear it (`lv_inv_area(disp, NULL)`) isn't bound either. On the badge the original line still runs. |

## How sim.py works

Every piece of hardware is created when its module is imported, so `sim.py` installs its fakes in `sys.modules` before importing any firmware.

| Hardware | Firmware | In the sim |
|---|---|---|
| Buttons (GPIO, polled every 1 ms) | `lib/button.py` | Fake `machine.Pin`. Each button's pin reads the sim's state for that button (keyboard or mouse). The sim finds the pins through the firmware's own `Button` objects, so it doesn't repeat pin numbers. |
| WS2812 LEDs | `lib/led_bar.py` via `neopixel` | Fake `neopixel` module. `write()` sends the colors to the window. |
| Status LED, battery sense | `lib/board.py` | The status LED's pin is shown as an indicator. The ADC reads ~2.35 V, which is USB power through the 1/2 divider. |
| E-paper panel (SoftSPI + pins) | `lib/epd.py` | The firmware's LVGL display, flush callback and framebuffers all run unchanged. The sim wraps `display.update()` to copy the black and red framebuffers into the window. With `--real-refresh`, a fake SPI bus watches for the panel's refresh command (`0x15`). The BUSY pins (active low) then read busy for the refresh time, and the firmware's own code waits on them as it does on the badge. |
| SAO I2C bus | `lib/sao.py` | Fake `machine.I2C` that forwards transactions to SAO emulators over TCP. `machine.I2CTarget` is accepted but does nothing. |
| Filesystem | everything | `firmware/badge/` is mounted at `/` and `firmware/sim/data/` at `/data`, using MicroPython's `VfsPosix`. Paths and `sys.path` match the badge. |

### Two LVGL displays

On the badge, LVGL has one display: the firmware's 960×672 e-paper display. **The firmware never runs LVGL's timers.** It redraws only when it calls `lvgl.refr_now(display.lv_display)`, and then calls `display.update()`.

The sim adds a second LVGL display for its window (`sdl_display`, with `sdl_pointer` for the mouse), and it does run LVGL's timers. Three details keep the two displays apart:

- **The firmware's display stays the default display**, because apps call `lvgl.obj()`, `lvgl.screen_active()` and `lvgl.layer_top()` without naming a display. The mouse driver attaches to the default display, so the sim switches the default to its window only while creating it.
- **The timers must not redraw the firmware's display on their own.** LVGL resumes a display's refresh timer whenever anything on it changes, so pausing the timer doesn't stick. Instead the sim sets its period to about 25 days. `refr_now()` calls the refresh function directly, so it still works.
- **The window's layout is the sim's own.** On resize, LVGL's resolution follows the window. The sim scales a fixed 1008×880 "badge face" to fit, centers it, and leaves black around it. The e-paper image is an RGB565 image stretched to its scaled box.

### Gotchas

- **Heap.** The window's two frame buffers come from the MicroPython heap (3 bytes per window pixel each, so about 23 MB each when maximized on a large screen). That's why `badge-sim` passes `-X heapsize=160M`. As a result, `micropython.mem_info()` in the firmware's heartbeat shows the sim's heap, not the badge's. The firmware itself uses about 10 MB (mostly LVGL's 1.3 MB draw buffer and the framebuffers).
- **Byte order.** LVGL stores RGB888 pixels as B,G,R bytes, but `sdl_display` tells SDL they're R,G,B unless it's given `color_byte_order=BYTE_ORDER_BGR`. Without it, red and blue swap.
- **Touch calibration file.** `sdl_pointer` normally saves a touch calibration file in the current directory, which would be the firmware's root. The sim passes it a calibration object that does nothing.
- **The window is double-buffered** (LVGL's DIRECT mode). Each buffer only holds the areas drawn into it, so to inspect what's on screen, read the buffer that was flushed last.
- **Private attributes are used:** `TaskHandler._timer`/`_task_handler` and `SDLDisplay._disp_drv`/`_windows_event_cb` from lvgl_micropython, and the firmware's `display.black`/`.red` framebuffers and `epd.EPD_*_PIN` module globals.

## SAO bus over TCP

All three SAO headers share one I2C bus, so the sim has one bus too. Each emulated SAO is a program that connects to `127.0.0.1:47100` and says which addresses it answers. The sim then forwards every transaction to that address over the connection. The messages are lines of ASCII, with bytes in hex:

```
SAO -> sim:  SAO <addr> [<addr> ...]       once, on connect, e.g. "SAO 50"
sim -> SAO:  I2C <addr> <write> <nread>    write the bytes (or "-"), then read nread
                                           bytes with a repeated start (if nread > 0)
SAO -> sim:  OK <read>                     the bytes read (or "-")
             NACK                          the device didn't acknowledge
```

All of `machine.I2C`'s methods map onto that one transaction: `scan`, `writeto`, `writevto`, `readfrom`, `readfrom_into`, `writeto_mem`, `readfrom_mem` and `readfrom_mem_into`.

- An address with no connected SAO raises `OSError(19)` (ENODEV), as on an empty bus.
- A NACK also raises ENODEV.
- If the SAO doesn't answer within 1 s, the sim raises ETIMEDOUT and disconnects it.
- SAOs can connect and disconnect while the sim runs. A new connection for an address replaces the old one.
- The SAO GPIO pins aren't simulated.

## Known gaps

- **Different MicroPython+LVGL build from the badge.** The badge is built with lv_micropython (see `firmware/README.md`); the sim runs on lvgl_micropython's Unix port, because that's where its SDL window and mouse drivers come from. Both use LVGL 9 and MicroPython 1.27, but their Python bindings differ in places. For example, lv_micropython exposes LVGL struct fields like `inv_p`, and lvgl_micropython doesn't. Code that runs in one can fail in the other. Porting the sim to lv_micropython's Unix port, which has its own SDL support, would remove this difference.
- **`lib/sao.py`'s `Sao.set_address()` ignores its argument** and sets the address to 0. It also creates an `I2CTarget` (the badge as a target) with the same address it then talks to as a controller. That module looks unfinished, so it was left alone. Using `lib.sao.i2c` directly works.
- **E-paper realism:** a refresh doesn't flash, there's no ghosting, and partial refresh isn't modeled. In `--real-refresh` mode, whether an image counts as "BWR" is decided by whether any red pixel is set. The firmware always sends both planes, so on the real panel every refresh may take the long time; adjust `--bw-time` if so. The refresh time is on top of the ~1.5 s of delays in the firmware's own update sequence.
- **Fidelity:** ESP32-S3 speed, RAM limits and timing aren't reproduced. The firmware's pure-Python LVGL flush and PNG decoding run much faster here than on the badge.
- **Mouse wheel and keyboard focus** in the window only drive the sim's buttons. The firmware has no LVGL input devices.
