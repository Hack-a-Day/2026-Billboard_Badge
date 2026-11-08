# Biomorph

Partial-refresh Pickover stalk / orbit-trap animation for the `E2969JS0B1` (960x672 B/W/R) panel.

Evaluates a 240x168 complex dynamical system in 12-bit fixed-point arithmetic (`@micropython.viper`), tracking the minimum distance of each point's trajectory across up to 10 iterations to the coordinate axes and an inner ring (`min(|Re z|, |Im z|, ||z|^2 - r_0^2|/2)` with `r_0^2 = 2800/4096`), and expands each cell to 960x672 using 4x4 Bayer stipple patterns. The complex parameter `c(t)`, rotation, zoom, and contour phase step on each partial refresh.

## Install

```bash
mpremote cp biomorph.py :apps + reset
```

## Controls

- `User A`: Toggle between drift mode (default, outer phase band left at `GND` so prior frames fade across passes) and crisp mode (outer phase band driven to `VDL` White, LED bar background lit dim white).
- `User B`: Cycle dynamical recurrence:
  - `0`: Julia (`z_{n+1} = z_n^2 + c`)
  - `1`: Burning Ship (`z_{n+1} = (|Re z_n| + i|Im z_n|)^2 + c`)
  - `2`: Perturbed Julia (`z_{n+1} = z_n^2 + (Re z_n - i Im z_n)/4 + c`)
- `Boot`: Jump ahead in the parameter orbit (`step += 17`).

## Theory of Operation (vs. Stock `lib/epd.py`)

1. **Persistent HV session + 20-frame LUT (`0xA7` -> `0xB0`):**
   Stock `global_update()` resets the dual COGs, runs the full OTP waveform, and powers down the charge pumps on every update. `biomorph.py` reads the panel's 88-byte OTP block (`0x70`/`0xB9`), runs the `0xA7` OTP load sequence (`0x45 = 0x84`) once when opening a session, waits 125 ms before writing a 400-byte (20-frame) LUT to `0xB0`, and keeps the HV booster running across steps. Each subsequent step writes planes `0x10`/`0x11` and strobes `0x15`.

2. **Direct 4-rail LUT mapping (`(bit_0x11 << 1) | bit_0x10`):**
   The 20-frame LUT maps the two 1-bpp planes to the four source rails:
   - `00` (`Table 0`): `GND` (`0 V`)
   - `01` (`Table 1`): `VDH` (Black)
   - `10` (`Table 2`): `VDL` (White)
   - `11` (`Table 3`): `VDHR` (Red)
   Group 9 Phase B is set to `GND` across all four tables.

3. **Contour phase bands & stipple expansion:**
   Each 240x168 grid cell computes a 6-bit contour phase `((trap >> 3) + iter * 7 + phase) & 63` and maps it into five phase bands using 4x4 Bayer stipple densities (`0%`, `25%`, `50%`, `75%`, `100%`):
   - `0..11`: `VDH` Black (`50%`–`100%`)
   - `12..21`: `VDL` White clearing wave (`75%`–`100%`)
   - `22..37`: `VDHR` Red stalk (`75%`–`100%`)
   - `38..45`: `VDL` White trailing edge (`50%`–`75%`)
   - `46..63`: `GND` (`solid_mode = False`, letting previous frames fade across gate scans) or `VDL` White `100%` (`solid_mode = True`)

## Credits & References

- **Clifford A. Pickover**, *"Biomorphs: Computer Displays of Biological Forms Generated from Mathematical Feedback Loops"* (Computer Graphics Forum, 1986) & *Computers, Pattern, Chaos and Beauty* (1990): Biomorph & orbit-trap stalk methods.
- **[TheESTest/Supercon_2026_Badge_Firmware](https://github.com/TheESTest/Supercon_2026_Badge_Firmware)** (`badge_files/lib/epd_fast.py`): MicroPython fast-refresh session architecture, `make_lut()` / `_frames_per_group()` waveform builder, OTP reader, and Driver-B (`0x45 = 0x84`) initialization sequence.
- **[phthallo/supercon](https://github.com/phthallo/supercon)** (`firmware/lib/epd.py`, `firmware/lib/luts.py`): 20-frame register `0xB0` custom LUT structure (`off = 0`, 2-frame minimum group timing) and persistent-booster partial refresh technique on the badge.
- **[Hack-a-Day/2026-Billboard_Badge](https://github.com/Hack-a-Day/2026-Billboard_Badge)** (`firmware/esp32s3-itc97/src/epd97.cpp`, commit `6ba6dd8`) & **[PervasiveDisplays/EPD_Driver_GU_large](https://github.com/PervasiveDisplays/EPD_Driver_GU_large)**: Official C++ fast-mode driver and `lutb_base` (`data/rendered_luts.py`).
- **[@trandi (PR #4)](https://github.com/Hack-a-Day/2026-Billboard_Badge/pull/4)** & **[@FacioErgoSum (PR #3)](https://github.com/Hack-a-Day/2026-Billboard_Badge/pull/3)**: 20 MHz hardware `SPI(1)` upgrade and `@micropython.viper` single-pass dual-COG plane split + 256-byte LUT bit-reversal.
