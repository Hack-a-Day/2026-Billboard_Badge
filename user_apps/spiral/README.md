# Spiral

Partial-refresh screensaver for the `E2969JS0B1` (960x672 B/W/R) panel.

Steps a 48x48-pixel rectangular spiral clockwise from the outer border to the center across a 20x14 grid (280 byte-aligned tiles, 4 tiles per step), cycling Black -> Red -> White at ~650 ms/step (~580 ms panel drive + ~70 ms SPI).

## Install

```bash
mpremote cp user_apps/spiral/spiral.py :apps/spiral.py
```

## Controls

- `User A`: Toggle between snake mode (default, trailing tiles fade) and solid-fill mode (LED bar background lit dim yellow).
- `User B` / `Boot`: Clear to white and restart from the top-left corner.
- `Menu +` / `Menu -`: Shut down DC/DC rails and exit to the app menu.

## Theory of Operation (vs. Stock `lib/epd.py`)

1. **Persistent HV session + 20-frame LUT (`0xA7` -> `0xB0`):**
   Stock `global_update()` hard-resets the dual COGs, runs a 650-frame (~18.2 s) OTP waveform, and powers down the charge pumps on every frame. `spiral.py` runs the `0xA7` OTP trim load (`0x45 = 0x84`) once when opening a session, waits 125 ms for the internal OTP-to-SRAM DMA transfer to finish, overwrites `0xB0` with a 400-byte (20-frame) LUT, and leaves the HV booster running across steps. Each step then requires only writing planes `0x10`/`0x11` and strobing `0x15`.

2. **Direct 4-rail LUT mapping (`(bit_0x11 << 1) | bit_0x10`):**
   The 20-frame LUT exposes all four source driver rails per pixel:
   - `00` (`Table 0`): `GND` (`0 V`)
   - `01` (`Table 1`): `VDH` (`+15 V`, Black)
   - `10` (`Table 2`): `VDL` (`-15 V`, White)
   - `11` (`Table 3`): `VDHR` (`+7 V`, Red)
   Group 9 Phase B is grounded (`0 V`) across all four tables so source lines discharge before `BUSY` releases.

3. **Gate-drain kickback (`C_gd`) in Snake vs. Solid mode:**
   - **Snake mode (`solid_mode = False`):** Drives only the 4 advancing tiles per step (plus an 8-tile `VDL` white clearing lookahead ahead of the Red pass) and leaves all other tiles at `00` (`GND`). Because each step still scans all 672 gate lines (~1,400 gate pulses per 70-step pass), gate-drain parasitic capacitance (`C_gd`) bleeds charge off `0 V` pixels, relaxing trailing tiles into a multi-tone gradient tail.
   - **Solid mode (`solid_mode = True`):** Holds untouched tiles ahead of the wavefront on the previous pass's rail and filled tiles behind the wavefront on the current pass's rail on every step so static Black and Red regions do not gray out during repeated gate scans.

## Credits & References

- **[TheESTest/Supercon_2026_Badge_Firmware](https://github.com/TheESTest/Supercon_2026_Badge_Firmware)** (`badge_files/lib/epd_fast.py`): MicroPython fast-refresh session architecture, `make_lut()` / `_frames_per_group()` waveform builder, and Driver-B (`0x45 = 0x84`) OTP initialization sequence.
- **[phthallo/supercon](https://github.com/phthallo/supercon)** (`firmware/lib/epd.py`, `firmware/lib/luts.py`): 20-frame register `0xB0` custom LUT structure (`off = 0`, 2-frame minimum group timing) and persistent-booster partial refresh technique on the badge.
- **[Hack-a-Day/2026-Billboard_Badge](https://github.com/Hack-a-Day/2026-Billboard_Badge)** (`firmware/esp32s3-itc97/src/epd97.cpp`, commit `6ba6dd8`) & **[PervasiveDisplays/EPD_Driver_GU_large](https://github.com/PervasiveDisplays/EPD_Driver_GU_large)**: Official C++ fast-mode driver and `lutb_base` (`data/rendered_luts.py`).
- **[@trandi (PR #4)](https://github.com/Hack-a-Day/2026-Billboard_Badge/pull/4)** & **[@FacioErgoSum (PR #3)](https://github.com/Hack-a-Day/2026-Billboard_Badge/pull/3)**: 20 MHz hardware `SPI(1)` upgrade and `@micropython.viper` single-pass dual-COG plane split + 256-byte LUT bit-reversal.
