# Upstream comparison

Baseline: [Hack-a-Day/2026-Billboard_Badge](https://github.com/Hack-a-Day/2026-Billboard_Badge)
at commit [`9877d2f7eaba59f84bb1b3699e7bbd118f677c79`](https://github.com/Hack-a-Day/2026-Billboard_Badge/tree/9877d2f7eaba59f84bb1b3699e7bbd118f677c79),
retrieved November 8, 2026.

The local Python driver predates some current upstream UI and grayscale work.
Its pre-extraction diff against upstream was 319 added and 244 removed lines.
The `rendered_luts.py` files match. The local C source is the supplied V33b snake
snapshot, not an upstream C component.

| Source | Reused or adapted |
| --- | --- |
| Upstream `firmware/badge/lib/epd.py` | PDI reset, register sequence, two-controller geometry, native full-window bytes and wire bit order |
| Upstream `firmware/badge/data/rendered_luts.py` | B1 base waveform; release RED table is byte-for-byte identical |
| Upstream `firmware/badge/lib/epd_grayscale.py` | Three-tone selectors, green-channel thresholds 96/204, grayscale LUT derivation, six preparation and three drawing pulls, BUSY assertion/completion discipline |
| Local C `main/epd_panel.c` | 20 MHz SPI, parallel OTP loading, 20 ms strobes, BOLD waveform, controller reuse between fast passes and mask mapping |
| Local C `main/decay.c` | Per-pixel pulse-budget behavior; the new libraries implement it with independent packed counters |

The release removes the C driver's border-art dependency, global singleton state,
weak no-op panel stubs and disabled dirty-window experiment. It propagates SPI
errors, waits for actual completion, supports caller-selected pins and cleans up
resources. Red/monochrome waveform changes are applied even inside a powered
burst. A red-only mask can run without a pending monochrome change.

Slow grayscale always starts with a factory white refresh in this release. That
is more conservative than upstream's optimization that skips the factory clear
when it already knows the panel is monochrome. No gray selector is sent through
the factory color waveform. Upstream mixed red-plus-gray rendering and its UI
integration are not replaced by the standalone library contribution.

The PR is additive under `libraries/billboard_epd`. It keeps upstream's current
badge firmware intact. The separately supplied local app backport and its
regression tests live in this working copy's `Python/` tree; they are not a
wholesale replacement for upstream's newer `firmware/` tree.
