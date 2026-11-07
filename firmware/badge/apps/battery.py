"""Battery indicator. The LED bar is a live gauge, the e-paper shows the detail.

User A / User B: refresh the e-paper now
"""

import time

import lvgl

from apps.base_app import BaseApp
from lib.battery import LOW_PCT, is_usb, read_reg_in_volts, voltage_to_pct
from lib.button import UserA, UserB
from lib.epd import display, display_width
from lib.led_bar import NUM_LEDS, leds

SAMPLE_PERIOD_MS = 1000
AUTO_REFRESH_MIN_MS = 5 * 60 * 1000
"""E-paper refreshes are slow and distracting, so don't chase small changes."""
AUTO_REFRESH_DELTA_PCT = 5

_MID_PCT = 50

_WHITE = lvgl.color_hex(0xFFFFFF)
_BLACK = lvgl.color_hex(0x000000)
_RED = lvgl.color_hex(0xFF0000)
_LEVEL_FONT = lvgl.font_montserrat_48
_DETAIL_FONT = lvgl.font_montserrat_24

_BODY_W = 640
_BODY_H = 260
_BODY_Y = 120
_BODY_BORDER = 10
_BODY_GAP = 10
"""White space between the border and the fill."""
_NUB_W = 28
_NUB_H = 110
_BODY_X = (display_width - _BODY_W - _NUB_W) // 2
_FILL_MAX_W = _BODY_W - 2 * (_BODY_BORDER + _BODY_GAP)
_LEVEL_Y = _BODY_Y + _BODY_H + 40
_DETAIL_Y = _LEVEL_Y + 70


def _rect(parent, x, y, width, height):
    rect = lvgl.obj(parent)
    rect.remove_style_all()
    rect.set_pos(x, y)
    rect.set_size(width, height)
    return rect


def _label(parent, y, font):
    label = lvgl.label(parent)
    label.set_style_text_font(font, 0)
    label.set_style_text_color(_BLACK, 0)
    label.align(lvgl.ALIGN.TOP_MID, 0, y)
    return label


class BatteryApp(BaseApp):
    show_battery_icon = False  # This whole screen is already a battery indicator

    def __init__(self, name: str | None = None) -> None:
        super().__init__(name or "Battery")
        self.volts = None
        self.on_usb = False
        self.pct = 0
        self.needs_draw = False
        self.last_sample_ms = None
        self.drawn_pct = 0
        self.drawn_on_usb = False
        self.drawn_ms = 0
        self._scr = None
        self._fill = None
        self._level = None
        self._detail = None

    def _build(self):
        scr = lvgl.obj()
        scr.remove_style_all()
        scr.set_size(lvgl.pct(100), lvgl.pct(100))
        scr.set_style_bg_color(_WHITE, 0)
        scr.set_style_bg_opa(lvgl.OPA.COVER, 0)

        body = _rect(scr, _BODY_X, _BODY_Y, _BODY_W, _BODY_H)
        body.set_style_border_color(_BLACK, 0)
        body.set_style_border_width(_BODY_BORDER, 0)
        body.set_style_border_opa(lvgl.OPA.COVER, 0)

        nub_y = _BODY_Y + (_BODY_H - _NUB_H) // 2
        nub = _rect(scr, _BODY_X + _BODY_W, nub_y, _NUB_W, _NUB_H)
        nub.set_style_bg_color(_BLACK, 0)
        nub.set_style_bg_opa(lvgl.OPA.COVER, 0)

        inset = _BODY_BORDER + _BODY_GAP
        self._fill = _rect(
            scr, _BODY_X + inset, _BODY_Y + inset, _FILL_MAX_W, _BODY_H - 2 * inset
        )
        self._fill.set_style_bg_opa(lvgl.OPA.COVER, 0)

        self._level = _label(scr, _LEVEL_Y, _LEVEL_FONT)
        self._detail = _label(scr, _DETAIL_Y, _DETAIL_FONT)
        self._scr = scr

    def _sample(self):
        self.volts = read_reg_in_volts()
        self.on_usb = is_usb(self.volts)
        self.pct = voltage_to_pct(self.volts)
        print(f"Battery: {self.volts:.3f}V {self.pct}% usb={self.on_usb}")

    def _show_on_leds(self):
        if self.on_usb:
            leds.fill((0, 0, 2))
            return
        if self.pct <= LOW_PCT:
            color = (3, 0, 0)
        elif self.pct <= _MID_PCT:
            color = (3, 1, 0)
        else:
            color = (0, 2, 0)
        # Keep one LED lit at 0% so the bar never looks switched off.
        lit = max(1, (self.pct * NUM_LEDS + 99) // 100)
        for i in range(NUM_LEDS):
            leds[i] = color if i < lit else (0, 0, 0)
        leds.write()

    def _auto_refresh_due(self):
        if self.on_usb != self.drawn_on_usb:
            return True
        if self.on_usb or abs(self.pct - self.drawn_pct) < AUTO_REFRESH_DELTA_PCT:
            return False
        return time.ticks_diff(time.ticks_ms(), self.drawn_ms) >= AUTO_REFRESH_MIN_MS

    def _draw(self):
        if self._scr is None:
            self._build()

        if self.on_usb:
            # Level isn't known on USB, so draw an empty battery rather than a guess.
            self._fill.set_width(0)
            self._level.set_text("USB power")
            self._detail.set_text(f"{self.volts:.2f} V - battery can't be read on USB")
        else:
            self._fill.set_width(_FILL_MAX_W * self.pct // 100)
            self._fill.set_style_bg_color(_RED if self.pct <= LOW_PCT else _BLACK, 0)
            self._level.set_text(f"{self.pct}%")
            self._level.set_style_text_color(_RED if self.pct <= LOW_PCT else _BLACK, 0)
            self._detail.set_text(f"{self.volts:.2f} V")

        lvgl.screen_load(self._scr)
        # screen_load does nothing if this is still the active screen, but another
        # app may have drawn straight to the framebuffers since. Repaint it all.
        self._scr.invalidate()
        self._scr.update_layout()
        lvgl.refr_now(display.lv_display)
        display.update()

        self.drawn_pct = self.pct
        self.drawn_on_usb = self.on_usb
        self.drawn_ms = time.ticks_ms()
        self.needs_draw = False

    def run(self):
        if self.launched:
            self.needs_draw = True
            self.last_sample_ms = None
        if UserA.pressed() or UserB.pressed():
            self.needs_draw = True
            self.last_sample_ms = None

        # A refresh pulls the battery down and the display task owns the LEDs
        # meanwhile, so sit it out rather than show a sagging reading.
        if display.is_busy():
            return

        now = time.ticks_ms()
        if (
            self.last_sample_ms is not None
            and time.ticks_diff(now, self.last_sample_ms) < SAMPLE_PERIOD_MS
        ):
            return
        self.last_sample_ms = now

        self._sample()
        self._show_on_leds()
        if self.needs_draw or self._auto_refresh_due():
            self._draw()
