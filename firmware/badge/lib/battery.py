"""Battery reading, plus the small icon stamped over whatever app is running.

BAT_SENS (IO14) sits on a 10k/10k divider from the 3.3V regulator's input, which
comes after the USB diode and the battery PFET. With USB plugged in the PFET is
off, so we read USB minus a diode drop and the battery can't be measured at all.

Apps can opt out of the icon with show_battery_icon = False, see apps/base_app.py.
"""

import asyncio as aio

from lib.board import reg_in_v_sns
from lib.epd import display

DIVIDER_RATIO = 2.0
"""Regulator input volts per volt at the ADC. Tweak to calibrate against a meter."""
USB_MIN_V = 4.35
"""A LiPo tops out at 4.2V and USB arrives at about 4.7V, so above this it's USB."""
NUM_SAMPLES = 64
LOW_PCT = 20

ICON_BARS = 4
CHECK_PERIOD_MS = 3 * 60 * 1000
"""How often the monitor looks for a drop that needs a refresh to show."""

# Resting LiPo voltage to state of charge, highest first.
_DISCHARGE_CURVE = (
    (4.20, 100),
    (4.06, 90),
    (3.98, 80),
    (3.92, 70),
    (3.87, 60),
    (3.82, 50),
    (3.79, 40),
    (3.77, 30),
    (3.74, 20),
    (3.68, 10),
    (3.45, 5),
    (3.30, 0),
)


def voltage_to_pct(volts: float) -> int:
    """Estimate state of charge by interpolating the discharge curve."""
    prev_v, prev_pct = _DISCHARGE_CURVE[0]
    if volts >= prev_v:
        return prev_pct
    for v, pct in _DISCHARGE_CURVE[1:]:
        if volts >= v:
            return pct + round((volts - v) * (prev_pct - pct) / (prev_v - v))
        prev_v, prev_pct = v, pct
    return 0


def read_reg_in_volts() -> float:
    """Regulator input voltage, averaged to smooth out ADC noise."""
    total_uv = 0
    for _ in range(NUM_SAMPLES):
        total_uv += reg_in_v_sns.read_uv()
    return total_uv / NUM_SAMPLES * DIVIDER_RATIO / 1_000_000


def is_usb(volts: float) -> bool:
    return volts > USB_MIN_V


def pct_to_bars(pct: int) -> int:
    """Whole bars, rounded up so only a truly empty battery shows none."""
    return (pct * ICON_BARS + 99) // 100


class BatteryIcon:
    """Top-right corner icon, drawn by EPaperDisplay on each frame it sends."""

    # The display saves and restores this area, so it must be byte-aligned in x.
    x = 904
    y = 8
    w = 48
    h = 20

    def __init__(self) -> None:
        self.drawn_bars = None
        """Bars on the last icon sent to the panel. None if it said USB instead."""

    def wanted(self) -> bool:
        """Replaced in main.py, so the active app gets to decide."""
        return True

    def draw(self, black, red) -> None:
        volts = read_reg_in_volts()
        body_x = self.x + 3
        body_y = self.y + 2

        black.fill_rect(self.x, self.y, self.w, self.h, 0)
        red.fill_rect(self.x, self.y, self.w, self.h, 0)
        black.rect(body_x, body_y, 38, 16, 1)
        black.fill_rect(body_x + 38, body_y + 4, 3, 8, 1)

        if is_usb(volts):
            black.text("USB", body_x + 7, body_y + 4, 1)
            self.drawn_bars = None
            return

        bars = pct_to_bars(voltage_to_pct(volts))
        ink = red if bars <= 1 else black
        for i in range(bars):
            ink.fill_rect(body_x + 2 + i * 9, body_y + 2, 7, 12, 1)
        self.drawn_bars = bars


battery_icon = BatteryIcon()


async def monitor() -> None:
    """Refresh the panel if a static app's icon falls behind by a bar.

    Each refresh is a slow full-screen flash, so only a falling bar count on
    battery earns one. Everything else waits for the app's next frame.
    """
    reference_bars = None
    dropped = False
    while True:
        await aio.sleep_ms(CHECK_PERIOD_MS)
        try:
            # Reading during a refresh would catch the battery sagging under it.
            if display.is_busy() or not battery_icon.wanted():
                continue
            volts = read_reg_in_volts()
            if is_usb(volts):
                reference_bars = None
                dropped = False
                continue

            bars = pct_to_bars(voltage_to_pct(volts))
            if battery_icon.drawn_bars is not None:
                reference_bars = battery_icon.drawn_bars
            elif reference_bars is None:
                reference_bars = bars

            if bars >= reference_bars:
                dropped = False
            elif dropped:
                # Low on two checks in a row, so it isn't a brief dip under load.
                dropped = False
                display.update()
            else:
                dropped = True
        except Exception as ex:
            print(f"Battery monitor hit an exception {ex}")
