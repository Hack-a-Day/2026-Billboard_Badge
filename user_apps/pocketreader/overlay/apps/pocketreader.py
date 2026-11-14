"""Fast direct-framebuffer PocketReader XL prototype for the Billboard Badge."""

import asyncio as aio
import esp32
import framebuf
import machine
import os
import time

from apps.base_app import BaseApp
from lib.button import MenuMinus, MenuPlus, UserA, UserB, PRESSED_READ
from lib.epd import display, display_height, display_width
from lib.led_bar import leds
from lib import pocketreader_font_data as font_data
from lib import book_library
from lib import book_transfer
from lib import wifi_setup
from lib import pocketreader_sleep
from lib import starter_library
from lib.pocketreader_display import ReaderDisplay


PHYSICAL_WIDTH = display_width
PORTRAIT_WIDTH = display_height
PORTRAIT_HEIGHT = display_width
SLEEP_IMAGE_PATH = "/data/graphics/pocketreader-sleep.png"
UPDATE_CONFIG_PATH = "/data/pocketreader_config.json"
SLEEP_HOLD_MS = 3000
SETUP_HOLD_MS = 3000
EXIT_HOLD_MS = 3000
LIGHT_SLEEP_OPTIONS = (1, 5, 10, 0)  # Zero means the light stays on.
DEVICE_SLEEP_OPTIONS = (0, 5, 10, 30)  # Zero disables automatic sleep.
REFRESH_ROW = 8  # Eight book rows; the footer actions are the ninth navigation stop.
LIBRARY_ROW_Y = 96
LIBRARY_ROW_HEIGHT = 96
LIBRARY_FRAME_HEIGHT = 84
USER_B_PIN = 16
# ESP32-S3 GPIO16 uses the RTC X32N pad when waking from deep sleep.
# Its RTC pulls are separate from the regular GPIO pulls on this chip.
RTC_GPIO16_PAD = 0x600084C4
RTC_GPIO_PULLUP = 1 << 27
RTC_GPIO_PULLDOWN = 1 << 28

# The badge's Menu +/- silkscreen labels are reversed relative to the firmware
# object names. These aliases describe the physical controls seen by the user.
PhysicalMenuMinus = MenuPlus
PhysicalMenuPlus = MenuMinus

# The physical LED row becomes a top-to-bottom column when the badge is turned
# counter-clockwise. LED 7 is nearest the top and LED 0 nearest the bottom.
ROW_LED_MAP = (7, 6, 5, 4, 3, 2, 1, 0)
WIFI_DURATION_ITEMS = ("10 MINUTES", "30 MINUTES", "1 HOUR", "UNTIL I TURN IT OFF")

BLACK = 1
RED = 2  # Existing accent requests render as black in the monochrome UI.
WHITE = 0


def path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


_font_blob = None


def font_blob():
    global _font_blob
    if _font_blob is None:
        with open(font_data.FONT_PATH, "rb") as handle:
            _font_blob = bytearray(handle.read())
    return _font_blob


class PackedFont:
    """Clockwise-rotated glyphs ready for native framebuf blits."""

    def __init__(self, offset, line_height, advances):
        self.offset = offset
        self.line_height = line_height
        self.advances = advances
        self._offsets = []
        self._cache = {}
        position = offset
        row_bytes = line_height // 8
        for advance in advances:
            self._offsets.append(position)
            position += row_bytes * advance

    def measure(self, text):
        return sum(self.advance(character) for character in text)

    def advance(self, character):
        codepoint = ord(character)
        if codepoint < 32 or codepoint > 126:
            codepoint = ord("?")
        return self.advances[codepoint - 32]

    def glyph(self, character):
        codepoint = ord(character)
        if codepoint < 32 or codepoint > 126:
            codepoint = ord("?")
        index = codepoint - 32
        if index not in self._cache:
            advance = self.advances[index]
            size = (self.line_height // 8) * advance
            start = self._offsets[index]
            view = memoryview(font_blob())[start : start + size]
            self._cache[index] = framebuf.FrameBuffer(
                view,
                self.line_height,
                advance,
                framebuf.MONO_HLSB,
            )
        return self._cache[index], self.advances[index]


HACK16 = PackedFont(
    font_data.HACK16_OFFSET,
    font_data.HACK16_LINE_HEIGHT,
    font_data.HACK16_ADVANCES,
)
HACK24 = PackedFont(
    font_data.HACK24_OFFSET,
    font_data.HACK24_LINE_HEIGHT,
    font_data.HACK24_ADVANCES,
)
OPEN_DYSLEXIC24 = PackedFont(
    font_data.OPEN_DYSLEXIC24_OFFSET,
    font_data.OPEN_DYSLEXIC24_LINE_HEIGHT,
    font_data.OPEN_DYSLEXIC24_ADVANCES,
)
OPEN_DYSLEXIC18 = PackedFont(font_data.OPEN_DYSLEXIC18_OFFSET, font_data.OPEN_DYSLEXIC18_LINE_HEIGHT, font_data.OPEN_DYSLEXIC18_ADVANCES)
OPEN_DYSLEXIC42 = PackedFont(font_data.OPEN_DYSLEXIC42_OFFSET, font_data.OPEN_DYSLEXIC42_LINE_HEIGHT, font_data.OPEN_DYSLEXIC42_ADVANCES)
OPEN_DYSLEXIC30 = PackedFont(font_data.OPEN_DYSLEXIC30_OFFSET, font_data.OPEN_DYSLEXIC30_LINE_HEIGHT, font_data.OPEN_DYSLEXIC30_ADVANCES)
READER_SIZES = {"small": (OPEN_DYSLEXIC18, 24, 32), "medium": (OPEN_DYSLEXIC24, 32, 24), "large": (OPEN_DYSLEXIC30, 40, 19), "xl": (OPEN_DYSLEXIC42, 56, 14)}


def _reader_family(prefix):
    result = {}
    for label, size, spacing, lines in (('small', 18, 24, 32), ('medium', 24, 32, 24),
                                         ('large', 30, 40, 19), ('xl', 42, 56, 14)):
        name = prefix + str(size)
        result[label] = (PackedFont(getattr(font_data, name + '_OFFSET'),
                                    getattr(font_data, name + '_LINE_HEIGHT'),
                                    getattr(font_data, name + '_ADVANCES')), spacing, lines)
    return result


READER_FAMILIES = {'opendyslexic': READER_SIZES, 'ubuntu': _reader_family('UBUNTU'), 'tinos': _reader_family('TINOS')}


class PortraitCanvas:
    """Draw portrait coordinates directly into the physical panel bitplanes."""

    def __init__(self, black, red):
        self.black = black
        self.red = red

    @staticmethod
    def _physical_rect(x, y, width, height):
        # The board is held counter-clockwise: portrait content is rotated
        # clockwise into the panel's native 960 x 672 landscape framebuffer.
        return PHYSICAL_WIDTH - y - height, x, height, width

    def fill_rect(self, x, y, width, height, color=BLACK):
        if width <= 0 or height <= 0:
            return
        px, py, pw, ph = self._physical_rect(x, y, width, height)
        if color in (BLACK, RED):
            self.black.fill_rect(px, py, pw, ph, 1)
            self.red.fill_rect(px, py, pw, ph, 0)
        else:
            self.black.fill_rect(px, py, pw, ph, 0)
            self.red.fill_rect(px, py, pw, ph, 0)

    def rect(self, x, y, width, height, thickness=1, color=BLACK):
        self.fill_rect(x, y, width, thickness, color)
        self.fill_rect(x, y + height - thickness, width, thickness, color)
        self.fill_rect(x, y, thickness, height, color)
        self.fill_rect(x + width - thickness, y, thickness, height, color)

    def text(self, text, x, y, scale=1, color=BLACK, max_width=None, font=None):
        if y & 7:
            raise ValueError("bitmap text y must align to an 8-pixel boundary")
        text = str(text)
        font = font or (HACK16 if scale == 1 else HACK24)
        if max_width is not None:
            original = text
            while text and font.measure(text) > max_width:
                text = text[:-1]
            if text != original:
                while text and font.measure(text + "...") > max_width:
                    text = text[:-1]
                text += "..."
        if not text:
            return
        plane = self.black
        cursor = x
        physical_x = PHYSICAL_WIDTH - y - font.line_height
        for character in text:
            glyph, advance = font.glyph(character)
            # Glyph cells overlap vertically between tightly spaced lines.
            # Keep zero pixels transparent so they do not erase earlier text.
            plane.blit(glyph, physical_x, cursor, 0)
            cursor += advance

    def wordmark(self, x, y):
        if y & 7:
            raise ValueError("bitmap wordmark y must align to an 8-pixel boundary")
        start = font_data.WORDMARK_OFFSET
        end = start + font_data.WORDMARK_SIZE
        wordmark = framebuf.FrameBuffer(
            memoryview(font_blob())[start:end],
            font_data.WORDMARK_PHYSICAL_WIDTH,
            font_data.WORDMARK_PHYSICAL_HEIGHT,
            framebuf.MONO_HLSB,
        )
        physical_x = PHYSICAL_WIDTH - y - font_data.WORDMARK_PHYSICAL_WIDTH
        self.black.blit(wordmark, physical_x, x)

    def wrapped_text(
        self,
        text,
        x,
        y,
        width,
        scale=2,
        color=BLACK,
        line_gap=4,
        font=None,
    ):
        font = font or (HACK16 if scale == 1 else HACK24)
        words = text.split()
        lines = []
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if font.measure(candidate) <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)

        line_height = font.line_height + line_gap
        for index, line in enumerate(lines):
            self.text(line, x, y + index * line_height, scale, color, width, font)
        return len(lines) * line_height


class PocketReaderApp(BaseApp):
    """Direct-bitplane private catalog, cache, and pre-paginated EPUB reader."""

    def __init__(self, name=None):
        super().__init__(name or "PocketReader XL")
        self.sleep_ms = 25
        self.captures_menu_buttons = True
        self.exit_requested = False
        self.selected = 0
        self.detail = False
        self._bookmark_menu = False
        self._bookmark_cursor = 0
        self._bookmarks = []
        self.books = []
        self.all_books = list(self.books)
        self.library_page = 0
        self.reader = None
        self.reader_display = ReaderDisplay()
        self.current_page = 0
        self.current_word = 0
        self._reading_started = 0
        self._reading_turns = 0
        self._reading_qualified = False
        self._download_led_count = -1
        self._download_task = None
        self._download_active = False
        self._reading_refresh_pending = False
        self._library_refresh_pending = False
        self._library_idle_since = time.ticks_ms()
        self._library_lights_asleep = False
        self._idle_light_minutes = book_library.idle_light_minutes()
        self._light_brightness, self._availability_lights = book_library.light_settings()
        self._dirty = True
        self._small_update = False
        self._drawn_library_selection = None
        self._drawn_library_choice = 0
        self._drawn_menu = None
        self._drawn_screen = None
        self._menu_update_pending = False
        self._selection_update_pending = False
        self._page_update_pending = False
        self._battery_percent = None
        self._battery_usb = False
        self._battery_last_poll = 0
        self._battery_candidate = None
        self._battery_candidate_count = 0
        self._catalog_pending = False
        self._catalog_task = None
        self._refresh_active = False
        self._select_tracking = False
        self._select_long_fired = False
        self._bookmark_hold_ready = False
        self._select_pending = False
        self._suppress_select = False
        self._suppress_chord = False
        self._back_button_tracking = False
        self._back_long_fired = False
        self._reading_exit_hold_ready = False
        self._remove_prompt = False
        self._remove_yes = False
        self._bottom_menu = False
        self._settings_menu = False
        self._settings_cursor = 0
        self._led_menu = False
        self._sort_cursor = 0
        self._settings_inline = None
        self._settings_row_pending = 0
        self._sleep_options_menu = False
        self._sleep_cursor = 0
        self._font_options_menu = False
        self._font_cursor = 0
        self._family_options_menu = False
        self._family_cursor = 0
        self._reading_light_mode = book_library.reading_light_mode()
        self._quick_boot_enabled = book_library.quick_boot_enabled()
        self.reader_display.quick_boot = self._quick_boot_enabled
        self._toc_menu = False
        self._toc_cursor = 0
        self._chapters = []
        self._toc_chord_started = None
        self._toc_chord_suppressed = False
        self._toc_hold_ready = False
        self._reading_hold_state = None
        self._bottom_choice = 0
        self._bottom_led_level = -1
        self._sleep_button_tracking = False
        self._sleep_button_long_fired = False
        self._page_hold_state = None
        self._previous_tracking = False
        self._previous_long_fired = False
        self._setup_chord_tracking = False
        self._setup_chord_fired = False
        self._setup_requested = False
        self._setup_pending = False
        self._setup_task = None
        self._setup_cancel_requested = False
        self._setup_screen_update = False
        self._wifi_menu = False
        self._wifi_view = "main"
        self._wifi_cursor = 0
        self._wifi_networks = []
        self._wifi_chosen = ""
        self._wifi_network_cursor = 0
        self._wifi_delete_yes = False
        self._wifi_a_tracking = False
        self._wifi_a_long_fired = False
        self._wifi_task = None
        self._transfer_generation = book_transfer.generation()
        self._web_settings_generation = wifi_setup.settings_generation()
        self._wifi_request = None
        self._wifi_last_status = (False, False)
        self._suppress_back = False
        self._sleep_requested = False
        self._sleep_pending = False
        self._sleep_timer_minutes = book_library.sleep_timer_minutes()
        self._device_idle_since = time.ticks_ms()
        self._menu_sleep_started = None
        self._menu_sleep_ready = False
        self._exit_pending = False
        self.update_status = ""

    def run(self):
        if self.launched:
            self._close_reader()
            self.detail = False
            self._bookmark_menu = False
            self.exit_requested = False
            self._dirty = True
            self._load_cached_catalog()
            recent = book_library.most_recent_read(self.all_books)
            if recent is not None:
                self.library_page = recent // REFRESH_ROW
                self._show_library_page()
                self.selected = recent % REFRESH_ROW
            self._read_battery()
            self._battery_last_poll = time.ticks_ms()
            self._battery_candidate = None
            self._battery_candidate_count = 0
            self._catalog_pending = False
            self._select_tracking = False
            self._select_long_fired = False
            self._bookmark_hold_ready = False
            self._select_pending = False
            self._suppress_select = bool(UserA.value)
            self._suppress_chord = False
            self._back_button_tracking = False
            self._back_long_fired = False
            self._reading_exit_hold_ready = False
            self._remove_prompt = False
            self._bottom_menu = False
            self._bottom_led_level = -1
            self._sleep_button_tracking = False
            self._sleep_button_long_fired = False
            self._page_hold_state = None
            self._previous_tracking = False
            self._previous_long_fired = False
            self._setup_chord_tracking = False
            self._setup_chord_fired = False
            self._setup_requested = False
            self._setup_pending = False
            self._setup_task = None
            self._setup_cancel_requested = False
            self._wifi_menu = False
            self._wifi_view = "main"
            self._wifi_cursor = 0
            self._wifi_a_tracking = False
            self._wifi_a_long_fired = False
            self._wifi_request = None
            self._wifi_last_status = wifi_setup.radio_status()
            self._suppress_back = False
            self._sleep_requested = False
            self._sleep_pending = False
            self._sleep_timer_minutes = book_library.sleep_timer_minutes()
            self._device_idle_since = time.ticks_ms()
            self._menu_sleep_started = None
            self._menu_sleep_ready = False
            self._exit_pending = False
            self._library_refresh_pending = False
            self._drawn_screen = None
            self._library_idle_since = time.ticks_ms()
            self._library_lights_asleep = False
            self._idle_light_minutes = book_library.idle_light_minutes()
            self._light_brightness, self._availability_lights = book_library.light_settings()
            self._settings_menu = False
            self._sleep_options_menu = False
            self._font_options_menu = False
            self._family_options_menu = False
            self._reading_light_mode = book_library.reading_light_mode()
            self._quick_boot_enabled = book_library.quick_boot_enabled()
            self.reader_display.quick_boot = self._quick_boot_enabled
            self._toc_menu = False
            self._toc_chord_started = None
            self._toc_chord_suppressed = False
            self._toc_hold_ready = False
            self._reading_hold_state = None
            self._show_selection_led()
            resume = wifi_setup.take_hostname_resume()
            reconnect = wifi_setup.always_on_ssid() if resume is None else resume[0]
            duration = None if resume is None else resume[1]
            if reconnect and self._wifi_task is None:
                self._wifi_task = aio.create_task(self._connect_wifi(reconnect, duration))
            sleep_resume = pocketreader_sleep.take()
            power_resume = pocketreader_sleep.take_first_launch()
            self._restore_sleep_state(sleep_resume if sleep_resume is not None else power_resume)

        if not self.detail and self._transfer_generation != book_transfer.generation():
            self._transfer_generation = book_transfer.generation()
            self._load_cached_catalog()
            self._dirty = True

        if self._web_settings_generation != wifi_setup.settings_generation():
            self._web_settings_generation = wifi_setup.settings_generation()
            self._sleep_timer_minutes = book_library.sleep_timer_minutes()
            self._device_idle_since = time.ticks_ms()
            self._idle_light_minutes = book_library.idle_light_minutes()
            self._light_brightness, self._availability_lights = book_library.light_settings()
            self._reading_light_mode = book_library.reading_light_mode()
            self._quick_boot_enabled = book_library.quick_boot_enabled()
            self.reader_display.quick_boot = self._quick_boot_enabled
            self._load_cached_catalog()
            if self._setup_pending:
                self._setup_screen_update = True
            self._dirty = True

        if self.detail and self.reader and not self._reading_qualified:
            if time.ticks_diff(time.ticks_ms(), self._reading_started) >= 120000:
                self._reading_qualified = True
                book_library.mark_read(self.reader.entry["id"])

        if self._exit_pending:
            return

        self._update_library_idle_lights()

        if self._download_task is not None:
            self._device_idle_since = time.ticks_ms()
            return
        if self._catalog_task is not None or self._wifi_task is not None:
            self._device_idle_since = time.ticks_ms()

        if self._handle_menu_sleep_chord():
            return
        if self._update_device_sleep_timer():
            return

        if self._setup_pending:
            if PhysicalMenuPlus.pressed():
                self._setup_cancel_requested = True
                self._suppress_back = True
            if self._setup_screen_update and self.reader_display.ready():
                display.clear()
                self._draw_setup(PortraitCanvas(display.black, display.red))
                self.reader_display.queue_frame()
                self._setup_screen_update = False
            if self._setup_task is None and self.reader_display.ready():
                if self._setup_cancel_requested:
                    self._finish_setup()
                else:
                    self._setup_task = aio.create_task(self._run_setup_portal())
            return

        if self._sleep_pending:
            if self.reader_display.ready() and not UserB.value:
                if self.reader_display.error:
                    self.update_status = "SLEEP ERROR: " + self.reader_display.error
                    self._sleep_pending = False
                    self._dirty = True
                    self._show_selection_led()
                else:
                    try:
                        self._enter_deep_sleep()
                    except Exception as exc:
                        self.update_status = 'SLEEP ERROR: ' + str(exc)
                        self._sleep_pending = False
                        self._dirty = True
            return

        if self._sleep_requested and self.reader_display.ready():
            self._sleep_requested = False
            self._begin_sleep()
            return

        wifi_setup.expire_if_due()
        if self._catalog_pending and self._catalog_task is None and self._wifi_task is None:
            self._catalog_pending = False
            self._catalog_task = aio.create_task(self._refresh_catalog())
        if self._wifi_menu:
            self._run_wifi_menu()
            return

        if self._settings_menu:
            self._run_settings_menu()
            return

        if self._bottom_menu:
            self._pulse_bottom_led()

        # Back/cancel must be scanned even during the panel's long global
        # refresh, otherwise an ordinary press can be missed completely.
        setup_button_handled = self._handle_setup_chord()
        toc_button_handled = False
        if not setup_button_handled:
            toc_button_handled = self._handle_toc_chord()
        back_button_handled = self._handle_back_button()
        select_button_handled = False
        if not setup_button_handled:
            select_button_handled = self._handle_select_button()
        sleep_button_handled = False
        if not setup_button_handled and not toc_button_handled:
            sleep_button_handled = self._handle_sleep_button()
        previous_button_handled = False if toc_button_handled else self._handle_previous_button()
        if self._setup_requested and self.reader_display.ready():
            self._setup_requested = False
            self._begin_settings(from_chord=True)
            return
        # A long press can remain held after paging. Start the redraw as soon
        # as the panel is ready rather than waiting for the button release.
        if self._page_update_pending and self._dirty and self.reader_display.ready():
            self._draw()
            return
        if toc_button_handled and self._toc_menu and self._dirty and self.reader_display.ready():
            self._draw()
            return
        if (
            back_button_handled
            or setup_button_handled
            or toc_button_handled
            or select_button_handled
            or sleep_button_handled
            or previous_button_handled
            or self._setup_requested
            or self._sleep_requested
        ):
            return

        if not self.reader_display.ready():
            return

        if self._selection_update_pending and not self._dirty:
            self._selection_update_pending = False
            if (self.selected != self._drawn_library_selection or
                    (self.selected == REFRESH_ROW and self._bottom_choice != self._drawn_library_choice)):
                if not self._update_library_border():
                    self._dirty = True
                    self._small_update = False
                else:
                    return

        if self._menu_update_pending and not self._dirty:
            if self._flush_menu_selection():
                return

        if not self._dirty and self._update_wifi_indicator():
            return

        if not self._dirty and self._check_power_transition():
            return

        if self._reading_refresh_pending:
            self._reading_refresh_pending = False
            if self.detail and not self._toc_menu and not self._bookmark_menu:
                self._show_reading_progress()
        if self._library_refresh_pending:
            self._library_refresh_pending = False
            if not self.detail:
                self._show_selection_led()

        if self._dirty:
            self._draw()
            return


        if self._select_pending:
            self._select_pending = False
            if not self.detail:
                if self.selected == REFRESH_ROW:
                    if self._bottom_choice == 0:
                        self._small_update = False
                        self._show_selection_led()
                        self._catalog_pending = True
                    else:
                        self._begin_settings()
                else:
                    self._download_task = aio.create_task(self._open_selected())

    def _handle_back_button(self):
        """Dispatch physical Menu+ back/cancel despite swapped labels."""
        if self._suppress_back:
            if not PhysicalMenuPlus.value:
                self._suppress_back = False
            return True
        if PhysicalMenuPlus.value:
            if not self._back_button_tracking:
                self._back_button_tracking = True
                self._back_long_fired = False
            if not self._back_long_fired:
                if self.detail:
                    self._update_reading_hold_feedback('exit', PhysicalMenuPlus.pressed_ms())
                else:
                    self._update_page_hold_feedback(PhysicalMenuPlus.pressed_ms())
            if not self._back_long_fired and PhysicalMenuPlus.pressed_ms() >= EXIT_HOLD_MS:
                self._back_long_fired = True
                self._reading_exit_hold_ready = True
                if not self.detail:
                    self._set_page_hold_feedback('blue')
            return True

        if self._back_button_tracking:
            self._back_button_tracking = False
            if self.detail: self._clear_reading_hold_feedback('exit')
            else: self._clear_page_hold_feedback()
            if self._back_long_fired:
                self._back_long_fired = False
                if getattr(self, '_reading_exit_hold_ready', False):
                    self._reading_exit_hold_ready = False
                    self._save_reader_progress()
                    pocketreader_sleep.clear_open_book()
                    self._close_reader()
                    self._leave_app()
            elif self._toc_menu:
                self._toc_menu = False
                self.update_status = ""
                self._dirty = True
            elif self._bookmark_menu:
                self._bookmark_menu = False
                self.update_status = ""
                self._dirty = True
            elif self._remove_prompt:
                self._remove_prompt = False
                self._dirty = True
                self._small_update = False
                self._show_selection_led()
            elif self._bottom_menu:
                self._bottom_menu = False
                self._dirty = True
                self._small_update = False
                self._show_selection_led()
            elif self.detail:
                self._save_reader_progress()
                pocketreader_sleep.clear_open_book()
                self._close_reader()
                self.detail = False
                self._dirty = True
                self._show_selection_led()
            return True
        return False

    def _handle_setup_chord(self):
        """Hold User A + User B to open reader settings."""
        if self._suppress_chord:
            if not UserA.value and not UserB.value:
                self._suppress_chord = False
                UserA._pressed_state = PRESSED_READ
                UserB._pressed_state = PRESSED_READ
            return True
        if UserA.value and UserB.value:
            if not self._setup_chord_tracking:
                self._setup_chord_tracking = True
                self._setup_chord_fired = False
                self._clear_page_hold_feedback()
                self._clear_reading_hold_feedback('bookmark')
                self._bookmark_hold_ready = False
            self._sleep_button_tracking = False
            self._select_tracking = False
            self._select_pending = False
            if not self._setup_chord_fired:
                elapsed = min(UserA.pressed_ms(), UserB.pressed_ms())
                if self.detail: self._update_reading_hold_feedback('settings', elapsed)
                else: self._update_page_hold_feedback(elapsed)
            if (
                not self._setup_chord_fired
                and UserA.pressed_ms() >= SETUP_HOLD_MS
                and UserB.pressed_ms() >= SETUP_HOLD_MS
            ):
                self._setup_chord_fired = True
                if not self.detail:
                    self._set_page_hold_feedback('blue')
            return True
        if self._setup_chord_tracking:
            self._setup_chord_tracking = False
            if self._setup_chord_fired:
                self._setup_requested = True
            if self.detail: self._clear_reading_hold_feedback('settings')
            else: self._clear_page_hold_feedback()
            self._setup_chord_fired = False
            self._suppress_chord = True
            return True
        return False

    def _handle_toc_chord(self):
        """Hold physical Previous and Next together to show chapter navigation."""
        if not self.detail or self._bookmark_menu:
            return False
        previous, next_button = PhysicalMenuMinus.value, UserB.value
        if self._toc_chord_suppressed:
            if not previous and not next_button:
                self._toc_chord_suppressed = False
                self._toc_chord_started = None
                self._toc_hold_ready = False
                PhysicalMenuMinus._pressed_state = PRESSED_READ
                UserB._pressed_state = PRESSED_READ
            return True
        if previous and next_button:
            if self._toc_chord_started is None:
                self._toc_chord_started = time.ticks_ms()
                self._toc_hold_ready = False
            self._previous_tracking = False
            self._sleep_button_tracking = False
            elapsed = time.ticks_diff(time.ticks_ms(), self._toc_chord_started)
            self._update_reading_hold_feedback('toc', elapsed)
            if elapsed >= EXIT_HOLD_MS:
                self._toc_hold_ready = True
            return True
        if self._toc_chord_started is not None:
            self._clear_reading_hold_feedback('toc')
            if self._toc_hold_ready:
                self._toc_menu = True
                self._toc_cursor = 0
                position = int(self.reader.pages[self.current_page][2])
                for index, chapter in enumerate(self._chapters):
                    if chapter[0] > position:
                        break
                    self._toc_cursor = index
                self.update_status = ""
                self._dirty = True
            self._toc_chord_suppressed = True
            return True
        return False

    def _handle_select_button(self):
        """Short User A selects; a long hold offers local download removal."""
        if self._suppress_select:
            if not UserA.value:
                self._suppress_select = False
            return False
        if UserA.value:
            if not self._select_tracking:
                self._select_tracking = True
                self._select_long_fired = False
            if self.detail and self.reader and not self._toc_menu and not self._bookmark_menu and not self._select_long_fired:
                self._update_reading_hold_feedback('bookmark', UserA.pressed_ms())
            elif not self.detail and not self._bottom_menu and not self._remove_prompt and self.selected != REFRESH_ROW and not self._select_long_fired:
                self._update_page_hold_feedback(UserA.pressed_ms(), 'red')
            if not self._select_long_fired and UserA.pressed_ms() >= EXIT_HOLD_MS:
                self._select_long_fired = True
                UserA._pressed_state = PRESSED_READ
                if self.detail and self.reader and not self._toc_menu:
                    self._bookmark_hold_ready = True
            return True
        if self._select_tracking:
            if self.detail: self._clear_reading_hold_feedback('bookmark')
            else: self._clear_page_hold_feedback()
            if self._select_long_fired and getattr(self, '_bookmark_hold_ready', False):
                self._bookmark_hold_ready = False
                self._bookmarks = book_library.bookmarks(self.reader.entry["id"])
                self._bookmark_cursor = 0
                self._bookmark_menu = True
                self.update_status = ""
                self._dirty = True
            elif self._select_long_fired and not self.detail and not self._bottom_menu and not self._remove_prompt and self.selected != REFRESH_ROW:
                if book_library.has_download(self.books[self.selected]):
                    self._remove_prompt = True
                    self._remove_yes = False
                else:
                    self.update_status = "BOOK IS NOT DOWNLOADED"
                self._dirty = True
                self._small_update = True
            elif not self._select_long_fired:
                if self.detail and self.reader:
                    if self._toc_menu:
                        if self._chapters:
                            self.current_page = self._page_for_word(self._chapters[self._toc_cursor][0])
                            self._save_reader_progress()
                        self._toc_menu = False
                        self.update_status = ""
                    elif self._bookmark_menu:
                        if self._bookmarks:
                            word = self._bookmarks[self._bookmark_cursor]
                            self.current_page = self._page_for_word(word)
                            self._save_reader_progress()
                        self._bookmark_menu = False
                        self.update_status = ""
                    else:
                        try:
                            word = int(self.reader.pages[self.current_page][2])
                            positions = [item for item in self._bookmarks
                                         if self._page_for_word(item) == self.current_page]
                            if positions:
                                book_library.remove_bookmarks(self.reader.entry["id"], positions)
                                self.update_status = "BOOKMARK REMOVED"
                            else:
                                book_library.add_bookmark(self.reader.entry["id"], word)
                                self.update_status = "BOOKMARK SAVED"
                            self._bookmarks = book_library.bookmarks(self.reader.entry["id"])
                        except Exception as exc:
                            self.update_status = "BOOKMARK ERROR: " + str(exc)
                    self._dirty = True
                elif self._remove_prompt:
                    if self._remove_yes:
                        try:
                            book_library.remove_download(self.books[self.selected])
                            self.update_status = "DOWNLOAD REMOVED"
                        except Exception as exc:
                            self.update_status = "REMOVE ERROR: " + str(exc)
                    self._remove_prompt = False
                    self._dirty = True
                    self._small_update = False
                    self._show_selection_led()
                else:
                    self._select_pending = True
            self._select_tracking = False
            self._select_long_fired = False
            return True
        return False

    def _handle_sleep_button(self):
        """Short press moves forward; a long hold advances a library page."""
        if UserB.value:
            if not self._sleep_button_tracking:
                self._sleep_button_tracking = True
                self._sleep_button_long_fired = False
            if not self.detail and not self._bottom_menu and not self._remove_prompt and not self._sleep_button_long_fired:
                self._update_page_hold_feedback(UserB.pressed_ms())
            if (
                not self._sleep_button_long_fired
                and UserB.pressed_ms() >= SLEEP_HOLD_MS
            ):
                self._sleep_button_long_fired = True
                if not self.detail and not self._bottom_menu and not self._remove_prompt:
                    if self._move_library_page(1):
                        self._set_page_hold_feedback('blue')
            return True

        if self._sleep_button_tracking:
            long_fired = self._sleep_button_long_fired
            self._sleep_button_tracking = False
            self._sleep_button_long_fired = False
            self._clear_page_hold_feedback()
            if not long_fired:
                self._dispatch_user_b_short()
            return True
        return False

    def _handle_previous_button(self):
        """Handle physical Menu- before the display-refresh gate."""
        if PhysicalMenuMinus.value:
            if not self._previous_tracking:
                self._previous_tracking = True
                self._previous_long_fired = False
            if not self.detail and not self._bottom_menu and not self._remove_prompt and not self._previous_long_fired:
                self._update_page_hold_feedback(PhysicalMenuMinus.pressed_ms())
            if not self._previous_long_fired and PhysicalMenuMinus.pressed_ms() >= SLEEP_HOLD_MS:
                self._previous_long_fired = True
                if not self.detail and not self._bottom_menu and not self._remove_prompt:
                    if self._move_library_page(-1):
                        self._set_page_hold_feedback('blue')
            return True
        if not self._previous_tracking:
            return False
        self._previous_tracking = False
        self._clear_page_hold_feedback()
        if self._previous_long_fired:
            self._previous_long_fired = False
            return True
        if self._toc_menu:
            if self._chapters:
                self._toc_cursor = (self._toc_cursor - 1) % len(self._chapters)
                self._show_toc_leds()
                self._menu_selection_changed()
            return True
        if self._bookmark_menu:
            if self._bookmarks:
                self._bookmark_cursor = (self._bookmark_cursor - 1) % len(self._bookmarks)
                self._menu_selection_changed()
            return True
        if self._remove_prompt:
            self._remove_yes = not self._remove_yes
            self._dirty = True
            self._small_update = True
            return True
        if self._bottom_menu:
            self._bottom_choice = (self._bottom_choice - 1) % 2
            self._dirty = True
            self._small_update = True
            return True
        if self.detail:
            if self.reader and self.current_page > 0:
                self.current_page -= 1
                self.update_status = ""
                self._record_page_turn()
                self._save_reader_progress()
                self._dirty = True
            return True
        self._move_selection(-1)
        self._show_selection_led()
        return True

    def _dispatch_user_b_short(self):
        if self._toc_menu:
            if self._chapters:
                self._toc_cursor = (self._toc_cursor + 1) % len(self._chapters)
                self._show_toc_leds()
                self._menu_selection_changed()
            return
        if self._bookmark_menu:
            if self._bookmarks:
                self._bookmark_cursor = (self._bookmark_cursor + 1) % len(self._bookmarks)
                self._menu_selection_changed()
            return
        if self._remove_prompt:
            self._remove_yes = not self._remove_yes
            self._dirty = True
            self._small_update = True
            return
        if self._bottom_menu:
            self._bottom_choice = (self._bottom_choice + 1) % 2
            self._dirty = True
            self._small_update = True
            return
        if self.detail:
            if self.reader and self.current_page + 1 < len(self.reader.pages):
                self.current_page += 1
                self.update_status = ""
                self._record_page_turn()
                self._save_reader_progress()
                self._dirty = True
            return
        self._move_selection(1)

    def _move_selection(self, step):
        slots = [(index, 0) for index in range(len(self.books))] + [(REFRESH_ROW, 0), (REFRESH_ROW, 1)]
        try:
            current = slots.index((self.selected, self._bottom_choice if self.selected == REFRESH_ROW else 0))
        except ValueError:
            current = 0
        self.selected, self._bottom_choice = slots[(current + step) % len(slots)]
        # While a refresh is running, keep just the newest requested row.
        # The framebuffer stays untouched until the panel is ready again.
        if not self._dirty and self._drawn_library_selection is not None:
            if self.reader_display.ready():
                if not self._update_library_border():
                    self._dirty = True
                    self._small_update = False
            else:
                self._selection_update_pending = True
        else:
            self._selection_update_pending = False
            self._dirty = True
            self._small_update = True
        self._show_selection_led()

    def _update_library_border(self):
        previous = self._drawn_library_selection
        previous_choice = self._drawn_library_choice
        if previous == self.selected and (previous != REFRESH_ROW or previous_choice == self._bottom_choice):
            return True
        try:
            canvas = PortraitCanvas(display.black, display.red)
            if previous == REFRESH_ROW:
                self._draw_footer_border(canvas, previous_choice, False)
            else:
                self._draw_book_border(canvas, previous, False, clear=True)
            if self.selected == REFRESH_ROW:
                self._draw_footer_border(canvas, self._bottom_choice, True)
            else:
                self._draw_book_border(canvas, self.selected, True, clear=True)
            self.reader_display.queue_frame(partial=True)
            self._drawn_library_selection = self.selected
            self._drawn_library_choice = self._bottom_choice
            return True
        except Exception:
            self._drawn_library_selection = None
            return False

    def _load_cached_catalog(self):
        self._set_catalog(book_library.load_catalog())

    def _set_catalog(self, catalog):
        books = list(catalog.get("books", []))
        books = [entry for entry in books if book_library.has_download(entry)]
        known_remote = {entry.get('id') for entry in books}
        for item in starter_library.load_catalog():
            if item.get('id') not in known_remote:
                item = dict(item)
                item['package'] = item['id'] + '.prb'
                item['_starter'] = True
                books.append(item)
                known_remote.add(item['id'])
        local = book_library.local_books()
        local_by_id = {entry.get("id"): entry for entry in local}
        books = [local_by_id.get(entry.get("id"), entry) for entry in books]
        known = {entry.get("id") for entry in books}
        books.extend(entry for entry in local if entry.get("id") not in known)
        books = book_library.sort_books(books)
        self.all_books = books
        self.library_page = max(0, min(self.library_page, (len(self.all_books) - 1) // REFRESH_ROW))
        self._show_library_page()
        if self.selected != REFRESH_ROW and self.selected >= len(self.books):
            self.selected = 0 if self.books else REFRESH_ROW

    def _show_library_page(self):
        start = self.library_page * REFRESH_ROW
        self.books = self.all_books[start:start + REFRESH_ROW]

    def _select_first_on_library_page(self):
        self.selected = 0 if self.books else REFRESH_ROW
        self._bottom_choice = 0

    def _move_library_page(self, step):
        target = self.library_page + step
        last_page = (len(self.all_books) - 1) // REFRESH_ROW
        if target < 0 or target > last_page:
            return False
        self.library_page = target
        self._show_library_page()
        self.selected = 0
        self._dirty = True
        self._small_update = False
        self._page_update_pending = True
        self._show_selection_led()
        return True

    async def _open_selected(self):
        entry = self.books[self.selected]
        if "package" not in entry:
            self.update_status = "HOLD A+B, THEN SETTINGS > WIFI"
            self._dirty = True
            self._download_task = None
            return
        wifi_setup.begin_operation()
        try:
            saved = book_library.load_progress().get(entry["id"], {})
            previous_word = int(saved.get("word", 0))
            legacy_excerpt = None
            if book_library.has_download(entry):
                old = None
                try:
                    old = book_library.BookPackage(entry)
                    if old.header.get("schema") == 1:
                        old_page = max(0, min(int(saved.get("page", 0)), len(old.pages) - 1))
                        old_lines, _ = old.read_page(old_page)
                        for line in old_lines:
                            line = line.strip()
                            if line and not (line.lower().startswith("page ") and line[5:].strip().isdigit()):
                                legacy_excerpt = line
                                break
                except Exception:
                    pass
                finally:
                    if old:
                        old.close()
            if entry.get('_starter') and not book_library.has_download(entry):
                self.update_status = "DOWNLOADING " + entry['title']
                self._download_active = True
                animation = aio.create_task(self._animate_download())
                try:
                    await starter_library.download(entry)
                finally:
                    self._download_active = False
                    await animation
            package = entry.get("indexed") or entry.get("dynamic")
            if package:
                entry = dict(entry)
                entry.update(package)
            elif book_library.has_download(entry):
                # Legacy downloads remain readable offline until they can be
                # replaced with a single reflowable book.
                cached = book_library.installed_state().get(entry["id"], {})
                entry = dict(entry)
                entry["sha256"] = cached.get("sha256")
            else:
                raise book_library.LibraryError("Refresh library to get this book")
            if book_library.has_download(entry):
                entry = dict(entry)
                entry["sha256"] = book_library.installed_state().get(entry["id"], {}).get("sha256")
            self.reader = book_library.BookPackage(entry)
            reader_size = book_library.text_size()
            reader_family = book_library.font_family()
            needs_index = not self.reader.has_index(reader_size, reader_family)
            animation = None
            if needs_index:
                self._download_active = True
                animation = aio.create_task(self._animate_download((0, 0, 2)))
            try:
                if animation is not None:
                    await aio.sleep_ms(0)
                await self.reader.prepare(reader_size, reader_family)
            finally:
                if animation is not None:
                    self._download_active = False
                    await animation
            self._chapters = self.reader.chapters()
            saved_word = previous_word
            if legacy_excerpt and self.reader.header.get("schema") in (2, 3):
                mapped = await self.reader.locate_excerpt(legacy_excerpt, previous_word)
                if mapped is not None:
                    saved_word = mapped
            self.current_page = 0
            for page_index, page_info in enumerate(self.reader.pages):
                if int(page_info[2]) > saved_word:
                    break
                self.current_page = page_index
            self.detail = True
            book_transfer.set_open_book(entry["id"])
            book_library.save_progress(entry["id"], self.current_page,
                                       int(self.reader.pages[self.current_page][2]))
            book_library.mark_opened(entry["id"])
            try:
                pocketreader_sleep.save_open_book(entry["id"])
            except OSError as exc:
                print("Could not save open book:", exc)
            self._bookmarks = book_library.bookmarks(self.reader.entry["id"])
            self._reading_started = time.ticks_ms()
            self._reading_turns = 0
            self._reading_qualified = False
            self._dirty = True
            self.update_status = "NO CHAPTER INDEX" if self._reading_light_mode == "chapter" and not self._chapters else ""
            leds.clear()
        except Exception as exc:
            self._close_reader()
            self.update_status = "BOOK ERROR: " + str(exc)
            self._dirty = True
            self._show_selection_led()
        finally:
            wifi_setup.end_operation()
            self._download_task = None

    async def _animate_download(self, color=(2, 2, 2)):
        index = 0
        while self._download_active:
            self._update_wifi_indicator()
            leds.pause()
            level = self._light_level()
            background = (max(1, level // 2), max(1, level // 2), 0) if level and self._availability_lights and color == (2, 2, 2) else (0, 0, 0)
            for led_index in ROW_LED_MAP:
                leds[led_index] = background
            leds[ROW_LED_MAP[index]] = tuple(level if component else 0 for component in color)
            leds.write()
            index = (index + 1) % len(ROW_LED_MAP)
            await aio.sleep_ms(150)

    async def _refresh_catalog(self):
        wifi_setup.begin_operation()
        try:
            if starter_library.enabled():
                await starter_library.refresh()
            self._load_cached_catalog()
            self.update_status = "LIBRARY UPDATED"
        except Exception as exc:
            self._load_cached_catalog()
            self.update_status = "LIBRARY UNAVAILABLE: " + str(exc)
        finally:
            wifi_setup.end_operation()
            if not self.detail and not self._settings_menu and not self._wifi_menu:
                self._select_first_on_library_page()
                self._show_selection_led()
            self._dirty = True
            self._small_update = False
            self._catalog_task = None

    async def _animate_refresh(self):
        index = 0
        while self._refresh_active:
            self._update_wifi_indicator()
            leds.pause()
            level = self._light_level()
            dim = max(1, level // 2) if level else 0
            for led_index in ROW_LED_MAP:
                leds[led_index] = (dim, dim, 0)
            leds[ROW_LED_MAP[index]] = (level, level, 0)
            leds.write()
            index = (index + 1) % len(ROW_LED_MAP)
            await aio.sleep_ms(150)

    def _show_download_progress(self, received, total):
        """Fill the portrait LED column from top to bottom as bytes arrive."""
        count = min(len(ROW_LED_MAP), received * len(ROW_LED_MAP) // total)
        if count == self._download_led_count:
            return
        self._download_led_count = count
        leds.pause()
        level = self._light_level()
        dim = max(1, level // 2) if level else 0
        for index, led_index in enumerate(ROW_LED_MAP):
            leds[led_index] = (0, 0, level) if index < count else (0, 0, dim)
        leds.write()

    def _show_connecting(self):
        leds.pause()
        level = self._light_level()
        leds.fill((level, level // 2, 0))

    def _show_rendering(self):
        leds.pause()
        leds.clear()

    def _show_reading_progress(self):
        """Show completed eighths of the current book or chapter while reading."""
        leds.pause()
        leds.fill((0, 0, 0))
        level = self._light_level()
        if not self.reader or not level or self._reading_light_mode == "off":
            leds.write()
            return
        pages = self.reader.pages
        total = max(1, int(self.reader.header.get("word_count", 1)))
        end = int(pages[self.current_page + 1][2]) if self.current_page + 1 < len(pages) else total
        end = max(0, min(total, end))
        start = 0
        if self._reading_light_mode == "chapter":
            if not self._chapters:
                leds.write()
                return
            chapter_index = -1
            for index, chapter in enumerate(self._chapters):
                if chapter[0] >= end:
                    break
                chapter_index = index
            if chapter_index < 0:
                leds.write()
                return
            start = self._chapters[chapter_index][0]
            total = self._chapters[chapter_index + 1][0] if chapter_index + 1 < len(self._chapters) else total
        span = max(1, total - start)
        done = max(0, min(span, end - start))
        count = min(8, (done * 8 + span - 1) // span)
        for index in range(count):
            leds[ROW_LED_MAP[index]] = (level, level, level)
        leds.write()

    def _show_toc_leds(self):
        leds.pause()
        leds.fill((0, 0, 0))
        level = self._light_level()
        count = min(8, max(0, len(self._chapters) - (self._toc_cursor // 8) * 8))
        if self._availability_lights:
            for index in range(count):
                leds[ROW_LED_MAP[index]] = (0, 0, level)
        if count:
            leds[ROW_LED_MAP[self._toc_cursor % 8]] = (level, level, level)
        leds.write()

    def _save_reader_progress(self):
        if not self.reader:
            return
        _, _, word = self.reader.pages[self.current_page]
        self.current_word = word
        book_library.save_progress(
            self.reader.entry["id"], self.current_page, self.current_word
        )

    def _record_page_turn(self):
        self._reading_turns += 1
        if self._reading_turns >= 2:
            self._reading_qualified = True
        if self._reading_qualified:
            book_library.mark_read(self.reader.entry["id"])

    def _page_for_word(self, target):
        result = 0
        for index, page in enumerate(self.reader.pages):
            if int(page[2]) > target:
                break
            result = index
        return result

    def _close_reader(self):
        self._bookmark_menu = False
        self._toc_menu = False
        self._chapters = []
        book_transfer.set_open_book(None)
        if self.reader:
            self.reader.close()
            self.reader = None

    def _begin_wifi_menu(self):
        self._wifi_menu = True
        self._wifi_view = "main"
        self._wifi_cursor = 0
        self._wifi_networks = wifi_setup.saved_networks()
        self._wifi_a_tracking = False
        self._wifi_a_long_fired = False
        self._suppress_chord = True
        self.update_status = ""
        self._dirty = True
        self._show_wifi_led()

    def _show_wifi_led(self):
        leds.pause()
        level = self._light_level()
        for led_index in ROW_LED_MAP:
            leds[led_index] = (0, 0, 0)
        count = self._wifi_item_count()
        if self._availability_lights and self._wifi_view == 'main':
            items = self._wifi_main_items()
            start = (self._wifi_cursor // 8) * 8
            for row, item in enumerate(items[start:start + 8]):
                if item is not None:
                    leds[ROW_LED_MAP[row]] = (0, 0, level)
        elif self._availability_lights:
            for led_index in ROW_LED_MAP[:min(8, count)]:
                leds[led_index] = (0, 0, level)
        if count:
            leds[ROW_LED_MAP[self._wifi_cursor % 8]] = (level, level, level)
        leds.write()

    def _run_wifi_menu(self):
        if self._suppress_chord:
            if not UserA.value and not UserB.value:
                self._suppress_chord = False
                UserA._pressed_state = PRESSED_READ
                UserB._pressed_state = PRESSED_READ
            return
        if PhysicalMenuPlus.pressed():
            if self._wifi_view == "delete":
                self._wifi_view = "main"
                self._wifi_delete_yes = False
            elif self._wifi_view == "duration":
                self._wifi_view = "main"
                self._wifi_cursor = self._wifi_network_cursor
            elif self._wifi_view == "login":
                self._wifi_view = "main"
                self._wifi_cursor = min(self._wifi_cursor, self._wifi_item_count() - 1)
                self._show_wifi_led()
            else:
                self._wifi_menu = False
                if not self.update_status.startswith("WIFI ERROR:"):
                    self.update_status = ""
                self._settings_menu = True
                self._settings_cursor = 5
                self._show_settings_led()
            self._dirty = True
            return
        step = -1 if PhysicalMenuMinus.pressed() else (1 if UserB.pressed() else 0)
        if step:
            if self._wifi_view == "delete":
                self._wifi_delete_yes = not self._wifi_delete_yes
                self._dirty = True
            else:
                count = self._wifi_item_count()
                if count:
                    self._wifi_cursor = (self._wifi_cursor + step) % count
                    if self._wifi_view == 'main':
                        items = self._wifi_main_items()
                        while items[self._wifi_cursor] is None:
                            self._wifi_cursor = (self._wifi_cursor + step) % count
                    self._show_wifi_led()
                    self._menu_selection_changed()
            return
        if UserA.value:
            if not self._wifi_a_tracking:
                self._wifi_a_tracking = True
                self._wifi_a_long_fired = False
            can_remove = (self._wifi_view == "main"
                          and self._wifi_main_items()[self._wifi_cursor] is not None
                          and self._wifi_main_items()[self._wifi_cursor][0] == 'network')
            if can_remove and not self._wifi_a_long_fired:
                self._update_page_hold_feedback(UserA.pressed_ms())
                if UserA.pressed_ms() >= EXIT_HOLD_MS:
                    self._wifi_a_long_fired = True
                    self._set_page_hold_feedback('blue')
            return
        if self._wifi_a_tracking:
            self._wifi_a_tracking = False
            self._clear_page_hold_feedback()
            if self._wifi_a_long_fired:
                self._wifi_view = "delete"
                self._wifi_delete_yes = False
                self._dirty = True
            else:
                self._select_wifi_item()
            self._wifi_a_long_fired = False
            return
        if self._menu_update_pending and self._flush_menu_selection():
            return
        if not self.reader_display.ready():
            return
        if self._dirty:
            self._draw()
            return
        self._update_wifi_indicator()

    def _select_wifi_item(self):
        if self._wifi_view == "delete":
            item = self._wifi_main_items()[self._wifi_cursor]
            if self._wifi_delete_yes and item is not None and item[0] == 'network':
                wifi_setup.remove_network(item[1]['ssid'])
                self._wifi_networks = wifi_setup.saved_networks()
                self._wifi_cursor = 0
                self.update_status = "NETWORK REMOVED"
            self._wifi_view = "main"
        elif self._wifi_view == "duration":
            if self._wifi_cursor < 4:
                self._queue_wifi_connection(self._wifi_chosen, (600000, 1800000, 3600000, None)[self._wifi_cursor])
            else:
                if self._wifi_task is not None or self._catalog_task is not None:
                    self.update_status = "WIFI BUSY"
                else:
                    wifi_setup.turn_off()
                    self.update_status = "WIFI OFF"
            self._wifi_view = "main"
            self._wifi_cursor = self._wifi_network_cursor
        elif self._wifi_view == "login":
            return
        elif self._wifi_cursor == 0:
            if self._wifi_task is not None or self._catalog_task is not None:
                self.update_status = "WIFI BUSY"
                self._dirty = True
                return
            if self.reader_display.ready():
                self._begin_setup()
                return
            self.update_status = "WAIT FOR DISPLAY"
        elif self._wifi_main_items()[self._wifi_cursor][0] == 'starter':
            starter_library.set_enabled(not starter_library.enabled())
            self._load_cached_catalog()
            if starter_library.enabled():
                self._catalog_pending = True
                self.update_status = "CONNECT WIFI TO UPDATE LIBRARY"
            else:
                self.update_status = "STARTER LIBRARY OFF"
        elif self._wifi_main_items()[self._wifi_cursor][0] == 'login':
            self._wifi_view = 'login'
        else:
            self._wifi_chosen = self._wifi_main_items()[self._wifi_cursor][1]['ssid']
            self._wifi_network_cursor = self._wifi_cursor
            self._wifi_view = "duration"
            self._wifi_cursor = 0
        self._dirty = True
        self._show_wifi_led()

    def _wifi_item_count(self):
        if self._wifi_view == "login":
            return 0
        if self._wifi_view == "duration":
            return len(WIFI_DURATION_ITEMS) + (1 if wifi_setup.radio_status()[0] else 0)
        return len(self._wifi_main_items())

    def _wifi_main_items(self):
        # Starter library and login occupy the last two rows.
        items = [('hotspot', None)] + [('network', item) for item in self._wifi_networks[:5]]
        while len(items) < 6:
            items.append(None)
        items.extend((('starter', None), ('login', None)))
        items.extend(('network', item) for item in self._wifi_networks[5:])
        return items

    def _menu_snapshot(self):
        """Identify a row-based menu and the row currently highlighted."""
        if self._wifi_menu:
            if self._wifi_view not in ('main', 'duration'):
                return None
            return (('wifi', self._wifi_view, self._wifi_cursor // 8), self._wifi_cursor % 8)
        if self._settings_menu:
            if self._sleep_options_menu:
                return (('settings', 'sleep'), self._sleep_cursor)
            if self._font_options_menu:
                return (('settings', 'font'), self._font_cursor)
            if self._family_options_menu:
                return (('settings', 'family'), self._family_cursor)
            view = 'led' if getattr(self, '_led_menu', False) else 'main'
            return (('settings', view), self._settings_cursor)
        if self._toc_menu and self._chapters:
            return (('toc', self._toc_cursor // 8), self._toc_cursor % 8)
        if self._bookmark_menu and self._bookmarks:
            return (('bookmarks', self._bookmark_cursor // 8), self._bookmark_cursor % 8)
        return None

    def _menu_selection_changed(self):
        if not self._dirty:
            self._menu_update_pending = True
            self._flush_menu_selection()

    def _flush_menu_selection(self):
        if not self._menu_update_pending or not self.reader_display.ready():
            return False
        self._menu_update_pending = False
        current, previous = self._menu_snapshot(), self._drawn_menu
        if current is None or previous is None or current[0] != previous[0]:
            self._dirty = True
            return False
        if current == previous:
            return False
        try:
            canvas = PortraitCanvas(display.black, display.red)
            for row, selected in ((previous[1], False), (current[1], True)):
                y = LIBRARY_ROW_Y + row * LIBRARY_ROW_HEIGHT
                canvas.rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT, 3, WHITE)
                canvas.rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT, 3 if selected else 1, BLACK)
            self.reader_display.queue_frame(partial=True)
            self._drawn_menu = current
            return True
        except Exception:
            self._drawn_menu = None
            self._dirty = True
            return False

    def _queue_wifi_connection(self, ssid, duration):
        if self._wifi_task is not None:
            self.update_status = "WIFI CONNECTING"
            return
        self.update_status = "CONNECTING " + ssid
        # Start independently of menu redraws: a quick exit or a busy panel
        # must not leave the connection request waiting in the Wi-Fi menu.
        self._wifi_task = aio.create_task(self._connect_wifi(ssid, duration))

    async def _connect_wifi(self, ssid, duration):
        try:
            # Give the user feedback before the asynchronous connection wait.
            if self._wifi_menu and self._dirty and self.reader_display.ready():
                self._draw()
            await wifi_setup.connect_saved(ssid, duration, self._update_wifi_indicator)
            if starter_library.enabled() and self._catalog_task is None:
                self._catalog_pending = True
            if self.update_status.startswith("CONNECTING ") or self.update_status.startswith("CONNECTED "):
                self.update_status = ""
        except Exception as exc:
            self.update_status = "WIFI ERROR: " + str(exc)
        finally:
            self._wifi_task = None
            self._dirty = True

    def _begin_setup(self):
        self._setup_pending = True
        self._setup_screen_update = False
        self._setup_task = None
        self._setup_cancel_requested = False
        leds.clear()
        display.clear()
        self._draw_setup(PortraitCanvas(display.black, display.red))
        self._drawn_screen = ('setup',)
        self.reader_display.queue_frame()

    async def _run_setup_portal(self):
        try:
            await wifi_setup.run_portal(lambda: self._setup_cancel_requested, self._update_wifi_indicator)
        except Exception as exc:
            self.update_status = "SETUP ERROR: " + str(exc)
        finally:
            self._finish_setup()

    def _finish_setup(self):
        self._setup_pending = False
        self._setup_task = None
        self._setup_cancel_requested = False
        self._back_button_tracking = False
        self._wifi_menu = True
        self._wifi_view = "main"
        self._wifi_cursor = 0
        self._wifi_networks = wifi_setup.saved_networks()
        self._dirty = True
        self._show_wifi_led()

    def _restore_sleep_state(self, sleep_resume):
        if sleep_resume:
            try:
                page = max(0, int(sleep_resume.get('library_page', 0)))
                self.library_page = min(page, max(0, (len(self.all_books) - 1) // REFRESH_ROW))
                book_id = sleep_resume.get('book_id')
                book_index = None
                if book_id:
                    for i, entry in enumerate(self.all_books):
                        if entry.get('id') == book_id:
                            book_index = i
                            break
                if book_index is not None:
                    self.library_page = book_index // REFRESH_ROW
                self._show_library_page()
                prior_selection = int(sleep_resume.get('selected', 0))
                self.selected = (book_index % REFRESH_ROW if book_index is not None else
                                 REFRESH_ROW if prior_selection == REFRESH_ROW else
                                 min(max(0, prior_selection), max(0, len(self.books) - 1)))
                self._bottom_choice = max(0, min(int(sleep_resume.get('bottom_choice', 0)), 2))
                if book_index is not None:
                    self._download_task = aio.create_task(self._open_selected())
                else:
                    if book_id:
                        self.update_status = 'LAST BOOK IS NOT AVAILABLE'
                    self._show_selection_led()
            except (TypeError, ValueError):
                self.update_status = 'COULD NOT RESTORE LAST BOOK'


    def _handle_menu_sleep_chord(self):
        if self._sleep_pending or self._setup_pending:
            return False
        both = PhysicalMenuMinus.value and PhysicalMenuPlus.value
        if both:
            if self._menu_sleep_started is None:
                self._menu_sleep_started = time.ticks_ms()
                self._menu_sleep_ready = False
                self._back_button_tracking = False
                self._back_long_fired = False
                self._reading_exit_hold_ready = False
                self._previous_tracking = False
                self._previous_long_fired = False
            elapsed = time.ticks_diff(time.ticks_ms(), self._menu_sleep_started)
            self._update_reading_hold_feedback('sleep', elapsed)
            if elapsed >= SLEEP_HOLD_MS:
                self._menu_sleep_ready = True
            return True
        if self._menu_sleep_started is not None:
            if PhysicalMenuMinus.value or PhysicalMenuPlus.value:
                return True
            PhysicalMenuMinus._pressed_state = PRESSED_READ
            PhysicalMenuPlus._pressed_state = PRESSED_READ
            self._clear_reading_hold_feedback('sleep')
            if self._menu_sleep_ready:
                self._sleep_requested = True
            self._menu_sleep_started = None
            self._menu_sleep_ready = False
            self._device_idle_since = time.ticks_ms()
            return True
        return False

    def _update_device_sleep_timer(self):
        now = time.ticks_ms()
        if (self._sleep_pending or self._sleep_requested or self._setup_pending
                or wifi_setup.radio_status()[0]
                or UserA.value or UserB.value or PhysicalMenuMinus.value or PhysicalMenuPlus.value):
            self._device_idle_since = now
            return False
        if self._sleep_timer_minutes and time.ticks_diff(now, self._device_idle_since) >= self._sleep_timer_minutes * 60000:
            self._sleep_requested = True
        return False

    def _begin_sleep(self):
        self._sleep_pending = True
        leds.clear()
        self.reader_display.queue_sleep_image(SLEEP_IMAGE_PATH)

    def _enter_deep_sleep(self):
        wake_pin = machine.Pin(USER_B_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
        if not wake_pin.value():
            raise ValueError('Release User B before sleeping')
        machine.mem32[RTC_GPIO16_PAD] = ((machine.mem32[RTC_GPIO16_PAD] | RTC_GPIO_PULLUP)
                                        & ~RTC_GPIO_PULLDOWN)
        if self.reader:
            self._save_reader_progress()
        book_id = self.reader.entry.get('id') if self.detail and self.reader else None
        pocketreader_sleep.save(book_id, self.library_page, self.selected, self._bottom_choice)
        esp32.wake_on_ext0(wake_pin, esp32.WAKEUP_ALL_LOW)
        machine.deepsleep()

    def _leave_app(self):
        leds.clear()
        self._exit_pending = True
        aio.create_task(self._finish_leave())

    async def _finish_leave(self):
        try:
            await self.reader_display.close()
        finally:
            self._exit_pending = False
            self.exit_requested = True

    def _set_page_hold_feedback(self, color):
        if color == 'blue':
            # The selected page may just have changed, so redraw the indicator.
            self._reading_hold_state = None
            self._update_reading_hold_feedback('page', 3000)

    def _update_page_hold_feedback(self, elapsed, active_color='white'):
        self._update_reading_hold_feedback('page_red' if active_color == 'red' else 'page', elapsed)

    def _clear_page_hold_feedback(self):
        self._clear_reading_hold_feedback('page')
        self._clear_reading_hold_feedback('page_red')

    def _update_reading_hold_feedback(self, owner, elapsed):
        # Light one more LED after each second; blue means the hold is ready.
        stage = min(3, max(0, elapsed // 1000))
        level = self._light_level()
        state = (owner, stage, level)
        if getattr(self, '_reading_hold_state', None) == state:
            return
        self._reading_hold_state = state
        leds.pause()
        leds.fill((0, 0, 0))
        for index in range(min(stage, 2)):
            leds[ROW_LED_MAP[index]] = (level, 0, 0) if owner == 'page_red' else (level, level, level)
        if stage >= 3:
            leds[ROW_LED_MAP[2]] = (0, 0, level)
        leds.write()

    def _clear_reading_hold_feedback(self, owner):
        state = getattr(self, '_reading_hold_state', None)
        if state is None or state[0] != owner:
            return
        self._reading_hold_state = None
        if self._toc_menu:
            self._show_toc_leds()
        elif self._bookmark_menu:
            self._show_rendering()
        elif self.detail:
            self._show_reading_progress()
        elif getattr(self, '_wifi_menu', False):
            self._show_wifi_led()
        elif getattr(self, '_settings_menu', False):
            self._show_settings_led()
        else:
            self._show_selection_led()

    def _show_selection_led(self):
        self._bottom_led_level = -1
        leds.pause()
        leds.fill((0, 0, 0))
        level = self._light_level()
        if not self._library_lights_asleep and level:
            if self._availability_lights:
                for index in range(REFRESH_ROW):
                    leds[ROW_LED_MAP[index]] = (0, 0, level) if index < len(self.books) else (level, 0, 0)
            if self.selected < REFRESH_ROW:
                leds[ROW_LED_MAP[self.selected]] = (level, 0, 0) if self._remove_prompt else (level, level, level)
        leds.write()

    def _light_level(self):
        return {"off": 0, "dim": 1, "medium": 2, "bright": 8}.get(self._light_brightness, 2)

    def _update_library_idle_lights(self):
        now = time.ticks_ms()
        on_library = (not self.detail and not self._wifi_menu and not self._bottom_menu and not self._settings_menu
                      and not self._remove_prompt and self._download_task is None
                      and self._catalog_task is None and self._wifi_task is None
                      and not self._setup_pending and not self._exit_pending)
        pressed = UserA.value or UserB.value or PhysicalMenuMinus.value or PhysicalMenuPlus.value
        if not on_library or pressed:
            self._library_idle_since = now
            if self._library_lights_asleep:
                self._library_lights_asleep = False
                if on_library:
                    self._show_selection_led()
        elif (self._idle_light_minutes and not self._library_lights_asleep
              and time.ticks_diff(now, self._library_idle_since) >= self._idle_light_minutes * 60000):
            self._library_lights_asleep = True
            self._show_selection_led()

    def _begin_settings(self, from_chord=False):
        self.update_status = ""
        self._bottom_menu = False
        self._settings_menu = True
        self._settings_cursor = 0
        self._led_menu = False
        self._settings_inline = None
        self._settings_row_pending = 0
        self._sleep_options_menu = False
        self._font_options_menu = False
        self._family_options_menu = False
        self._small_update = False
        if from_chord:
            self._setup_chord_tracking = False
            self._setup_chord_fired = False
            self._suppress_chord = True
        self._show_settings_led()
        self._dirty = True

    def _show_settings_led(self):
        leds.pause()
        leds.fill((0, 0, 0))
        cursor = self._sleep_cursor if self._sleep_options_menu else (self._font_cursor if self._font_options_menu else (self._family_cursor if self._family_options_menu else self._settings_cursor))
        level = self._light_level()
        if self._availability_lights and level:
            count = 3 if self._family_options_menu else (4 if self._sleep_options_menu or self._font_options_menu or getattr(self, '_led_menu', False) else 8)
            for index in range(count):
                leds[ROW_LED_MAP[index]] = (0, 0, level)
        leds[ROW_LED_MAP[cursor]] = (level, level, level)
        leds.write()

    def _queue_settings_rows(self, *rows):
        for row in rows:
            self._settings_row_pending |= 1 << row

    def _flush_settings_rows(self):
        if not self._settings_row_pending or not self.reader_display.ready():
            return False
        if not self._settings_menu or self._dirty or self._sleep_options_menu:
            return False
        pending = self._settings_row_pending
        try:
            canvas = PortraitCanvas(display.black, display.red)
            for row in range(8):
                if pending & (1 << row):
                    y = LIBRARY_ROW_Y + row * LIBRARY_ROW_HEIGHT
                    canvas.fill_rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT, WHITE)
                    self._draw_settings_row(canvas, row)
            self.reader_display.queue_frame(partial=True)
            self._settings_row_pending &= ~pending
            return True
        except Exception:
            self._settings_row_pending = 0
            self._dirty = True
            return False

    def _run_settings_menu(self):
        if self._suppress_chord:
            if not UserA.value and not UserB.value:
                self._suppress_chord = False
                UserA._pressed_state = PRESSED_READ
                UserB._pressed_state = PRESSED_READ
            return
        if PhysicalMenuPlus.pressed():
            if self._settings_inline:
                row = {'size': 0, 'family': 1, 'sort': 3}[self._settings_inline]
                self._settings_inline = None
                self._queue_settings_rows(row)
                self._flush_settings_rows()
                return
            if self._sleep_options_menu or self._font_options_menu or self._family_options_menu:
                self._sleep_options_menu = False
                self._font_options_menu = False
                self._family_options_menu = False
                self._show_settings_led()
            elif getattr(self, '_led_menu', False):
                self._led_menu = False
                self._settings_cursor = 2
                self._show_settings_led()
            else:
                self._settings_menu = False
                if self._toc_menu:
                    self._show_toc_leds()
                elif self._bookmark_menu:
                    self._show_rendering()
                elif self.detail:
                    self._show_reading_progress()
                else:
                    self._select_first_on_library_page()
                    self._show_selection_led()
            self._dirty = True
            self._small_update = False
            return
        step = -1 if PhysicalMenuMinus.pressed() else (1 if UserB.pressed() else 0)
        if step:
            if self._settings_inline == 'size':
                self._font_cursor = (self._font_cursor + step) % 4
                self._queue_settings_rows(0)
            elif self._settings_inline == 'family':
                self._family_cursor = (self._family_cursor + step) % 3
                self._queue_settings_rows(1)
            elif self._settings_inline == 'sort':
                self._sort_cursor = (self._sort_cursor + step) % 5
                self._queue_settings_rows(3)
            elif self._sleep_options_menu:
                self._sleep_cursor = (self._sleep_cursor + step) % len(LIGHT_SLEEP_OPTIONS)
            elif self._font_options_menu:
                self._font_cursor = (self._font_cursor + step) % 4
            elif self._family_options_menu:
                self._family_cursor = (self._family_cursor + step) % 3
            else:
                self._settings_cursor = (self._settings_cursor + step) % (4 if getattr(self, '_led_menu', False) else 8)
            if not self._settings_inline:
                self._show_settings_led()
                self._menu_selection_changed()
        elif UserA.pressed():
            full_refresh = False
            if self._settings_inline == 'size':
                book_library.set_text_size(('small', 'medium', 'large', 'xl')[self._font_cursor])
                self._settings_inline = None
                self._queue_settings_rows(0)
            elif self._settings_inline == 'family':
                book_library.set_font_family(('opendyslexic', 'ubuntu', 'tinos')[self._family_cursor])
                self._settings_inline = None
                # The font affects previews and subsequent rendering across the screen.
                self._settings_row_pending = 0
                full_refresh = True
            elif self._settings_inline == 'sort':
                _, grouped = book_library.library_sort_settings()
                book_library.set_library_sort_settings(('title', 'author_first', 'author_last', 'added', 'opened')[self._sort_cursor], grouped)
                self._settings_inline = None
                self._load_cached_catalog()
                self._queue_settings_rows(3)
            elif self._sleep_options_menu:
                self._idle_light_minutes = LIGHT_SLEEP_OPTIONS[self._sleep_cursor]
                book_library.set_idle_light_minutes(self._idle_light_minutes)
                self._sleep_options_menu = False
                self._show_settings_led()
                full_refresh = True
            elif self._font_options_menu:
                book_library.set_text_size(("small", "medium", "large", "xl")[self._font_cursor])
                self._font_options_menu = False
                self._show_settings_led()
                full_refresh = True
            elif self._family_options_menu:
                book_library.set_font_family(('opendyslexic', 'ubuntu', 'tinos')[self._family_cursor])
                self._family_options_menu = False
                self._show_settings_led()
                full_refresh = True
            elif getattr(self, '_led_menu', False) and self._settings_cursor == 0:
                levels = ('off', 'dim', 'medium', 'bright')
                self._light_brightness = levels[(levels.index(self._light_brightness) + 1) % len(levels)]
                self._show_settings_led()
                book_library.set_light_setting('light_brightness', self._light_brightness)
                self._queue_settings_rows(0)
            elif getattr(self, '_led_menu', False) and self._settings_cursor == 1:
                self._availability_lights = not self._availability_lights
                self._show_settings_led()
                book_library.set_light_setting('availability_lights', self._availability_lights)
                self._queue_settings_rows(1)
            elif getattr(self, '_led_menu', False) and self._settings_cursor == 2:
                modes = ('chapter', 'book', 'off')
                self._reading_light_mode = modes[(modes.index(self._reading_light_mode) + 1) % len(modes)]
                book_library.set_reading_light_mode(self._reading_light_mode)
                self._queue_settings_rows(2)
            elif getattr(self, '_led_menu', False):
                self._sleep_cursor = LIGHT_SLEEP_OPTIONS.index(self._idle_light_minutes)
                self._sleep_options_menu = True
                self._show_settings_led()
                full_refresh = True
            elif self._settings_cursor == 0:
                self._font_cursor = ("small", "medium", "large", "xl").index(book_library.text_size())
                self._settings_inline = 'size'
                self._queue_settings_rows(0)
            elif self._settings_cursor == 1:
                self._family_cursor = ('opendyslexic', 'ubuntu', 'tinos').index(book_library.font_family())
                self._settings_inline = 'family'
                self._queue_settings_rows(1)
            elif self._settings_cursor == 2:
                self._led_menu = True
                self._settings_cursor = 0
                self._show_settings_led()
                full_refresh = True
            elif self._settings_cursor == 3:
                order, _ = book_library.library_sort_settings()
                self._sort_cursor = ('title', 'author_first', 'author_last', 'added', 'opened').index(order)
                self._settings_inline = 'sort'
                self._queue_settings_rows(3)
            elif self._settings_cursor == 4:
                order, grouped = book_library.library_sort_settings()
                book_library.set_library_sort_settings(order, not grouped)
                self._load_cached_catalog()
                self._queue_settings_rows(4)
            elif self._settings_cursor == 5:
                self._settings_menu = False
                self._begin_wifi_menu()
                return
            elif self._settings_cursor == 7:
                self._sleep_timer_minutes = DEVICE_SLEEP_OPTIONS[(DEVICE_SLEEP_OPTIONS.index(self._sleep_timer_minutes) + 1) % len(DEVICE_SLEEP_OPTIONS)]
                book_library.set_sleep_timer_minutes(self._sleep_timer_minutes)
                self._device_idle_since = time.ticks_ms()
                self._queue_settings_rows(7)
            else:
                self._quick_boot_enabled = not self._quick_boot_enabled
                book_library.set_quick_boot_enabled(self._quick_boot_enabled)
                self.reader_display.quick_boot = self._quick_boot_enabled
                self._queue_settings_rows(6)
            if full_refresh:
                self._dirty = True
                self._small_update = False
        if self._menu_update_pending and self._flush_menu_selection():
            return
        if self._settings_row_pending and self._flush_settings_rows():
            return
        if self._dirty and self.reader_display.ready():
            self._draw()

    def _pulse_bottom_led(self):
        # Gentle pulse within the reduced white brightness used for selection.
        phase = (time.ticks_ms() // 220) % 4
        level = (0, max(1, self._light_level() // 2), self._light_level(), max(1, self._light_level() // 2))[phase] if self._light_level() else 0
        if level == self._bottom_led_level:
            return
        self._bottom_led_level = level
        leds.pause()
        # Footer actions are below the eight physical book LEDs.
        leds.write()

    def _read_battery(self):
        try:
            adc = machine.ADC(machine.Pin(14))
            adc.atten(machine.ADC.ATTN_11DB)
            readings = [adc.read_uv() for _ in range(8)]
            volts = sum(readings) * 2 / (len(readings) * 1000000)
            # The sensed rail is diode-fed by USB, not raw VBUS. Hysteresis
            # avoids switching back and forth near the boundary.
            threshold = 4.25 if self._battery_usb else 4.4
            self._battery_usb = threshold <= volts <= 5.5
            if self._battery_usb:
                self._battery_percent = None
            elif 2.8 <= volts <= 4.35:
                self._battery_percent = max(0, min(100, int((volts - 3.3) * 100 / 0.9)))
            else:
                self._battery_percent = None
        except Exception:
            self._battery_percent = None
            self._battery_usb = False

    def _check_power_transition(self):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._battery_last_poll) < 3000:
            return False
        self._battery_last_poll = now
        old_usb, old_percent = self._battery_usb, self._battery_percent
        self._read_battery()
        new_usb, new_percent = self._battery_usb, self._battery_percent
        # A failed measurement must not be interpreted as an unplug event.
        if new_percent is None and not new_usb:
            self._battery_usb, self._battery_percent = old_usb, old_percent
            self._battery_candidate = None
            self._battery_candidate_count = 0
            return False
        old_state = "usb" if old_usb else ("battery" if old_percent is not None else "unknown")
        new_state = "usb" if new_usb else "battery"
        if new_state == old_state:
            self._battery_usb, self._battery_percent = old_usb, old_percent
            self._battery_candidate = None
            self._battery_candidate_count = 0
            return False
        if new_state == self._battery_candidate:
            self._battery_candidate_count += 1
        else:
            self._battery_candidate = new_state
            self._battery_candidate_count = 1
        if self._battery_candidate_count < 2:
            self._battery_usb, self._battery_percent = old_usb, old_percent
            return False
        self._battery_candidate = None
        self._battery_candidate_count = 0
        canvas = PortraitCanvas(display.black, display.red)
        canvas.fill_rect(PORTRAIT_WIDTH - 48, 0, 48, 56, WHITE)
        self._draw_battery(canvas)
        self.reader_display.queue_frame(partial=True)
        return True

    def _draw_battery(self, canvas):
        x, y = PORTRAIT_WIDTH - 41, 8
        canvas.rect(x, y, 32, 16, 1, BLACK)
        canvas.fill_rect(x + 32, y + 5, 3, 6, BLACK)
        percent = self._battery_percent
        if percent is None:
            label = "USB" if self._battery_usb else "?"
            canvas.text(label, PORTRAIT_WIDTH - 6 - HACK16.measure(label), y + 16, 1, BLACK, 40)
            return
        count = 0 if percent < 5 else min(4, (percent - 1) // 25 + 1)
        for index in range(count):
            canvas.fill_rect(x + 2 + index * 7, y + 3, 5, 10, BLACK)
        label = "{}%".format(percent)
        canvas.text(label, PORTRAIT_WIDTH - 6 - HACK16.measure(label), y + 16, 1, BLACK, 40)

    def _draw_wifi_indicator(self, canvas):
        active, connected = wifi_setup.radio_status()
        self._wifi_last_status = (active, connected)
        if not active:
            return
        # A compact radio indicator; a solid center means connected.
        canvas.rect(600, 8, 25, 16, 1, BLACK)
        canvas.fill_rect(604, 12, 17, 2, BLACK)
        canvas.fill_rect(607, 16, 11, 2, BLACK)
        if connected:
            canvas.fill_rect(611, 20, 4, 3, BLACK)
        else:
            canvas.rect(611, 19, 4, 4, 1, BLACK)

    def _update_wifi_indicator(self):
        try:
            status = wifi_setup.radio_status()
            if status == self._wifi_last_status or self._dirty or not self.reader_display.ready():
                return False
            canvas = PortraitCanvas(display.black, display.red)
            canvas.fill_rect(598, 0, 30, 40, WHITE)
            self._draw_wifi_indicator(canvas)
            self.reader_display.queue_frame(partial=True)
            return True
        except Exception:
            return False

    def _screen_identity(self):
        if self._wifi_menu:
            return ('wifi', self._wifi_view, self._wifi_cursor // 8 if self._wifi_view == 'main' else 0)
        if self._settings_menu:
            if self._sleep_options_menu: return ('settings', 'sleep')
            if self._font_options_menu: return ('settings', 'size')
            if self._family_options_menu: return ('settings', 'family')
            if getattr(self, '_led_menu', False): return ('settings', 'led')
            return ('settings', 'main')
        if self._toc_menu:
            return ('toc', self._toc_cursor // 8)
        if self._bookmark_menu:
            return ('bookmarks', self._bookmark_cursor // 8)
        if self._remove_prompt:
            return ('remove',)
        if self._bottom_menu:
            return ('bottom',)
        if self.detail:
            return ('book',)
        return ('library', self.library_page)

    def _draw(self):
        screen = self._screen_identity()
        if screen != getattr(self, '_drawn_screen', None):
            self._small_update = False
        self._read_battery()
        display.clear()
        self._drawn_library_selection = None
        self._selection_update_pending = False
        self._page_update_pending = False
        canvas = PortraitCanvas(display.black, display.red)
        if self._wifi_menu:
            self._draw_wifi_menu(canvas)
        elif self._settings_menu:
            self._draw_settings(canvas)
            self._settings_row_pending = 0
        elif self._toc_menu:
            self._draw_toc(canvas)
        elif self._bookmark_menu:
            self._draw_bookmarks(canvas)
        elif self.detail:
            self._draw_detail(canvas)
        else:
            self._draw_library(canvas)
        self._draw_wifi_indicator(canvas)
        self._drawn_menu = self._menu_snapshot()
        self._menu_update_pending = False
        if self._wifi_menu:
            self._show_wifi_led()
        elif self._settings_menu:
            self._show_settings_led()
        elif self._toc_menu:
            self._reading_refresh_pending = False
            self._show_toc_leds()
        elif self.detail:
            if self._bookmark_menu:
                self._show_rendering()
            else:
                self._show_reading_progress()
            self._reading_refresh_pending = False
        else:
            self._library_refresh_pending = True
        # Experiment with differential updates for library selection changes.
        self.reader_display.queue_frame(partial=self._small_update and not self.detail and not self._wifi_menu)
        self._drawn_screen = screen
        self._small_update = False
        self._dirty = False

    def _draw_navigation_hints(self, canvas, reader=False):
        canvas.rect(4, 4, 60, 44, 1, BLACK)
        canvas.rect(68, 4, 60, 44, 1, BLACK)
        canvas.text("MENU-", 8, 8, 1, BLACK, 52)
        canvas.text("PREV" if reader else "UP", 8, 24, 1, BLACK, 52)
        canvas.text("USR-B", 72, 8, 1, BLACK, 52)
        canvas.text("NEXT" if reader else "DOWN", 72, 24, 1, BLACK, 52)
        if reader:
            canvas.text("HOLD BOTH FOR TOC", 4, 48, 1, BLACK, 170)

    def _draw_bottom_hints(self, canvas, reader=False):
        canvas.rect(4, 912, 60, 44, 1, BLACK)
        canvas.rect(68, 912, 60, 44, 1, BLACK)
        canvas.text("MENU+", 8, 920, 1, BLACK, 52)
        canvas.text("LIB" if reader and not self._bookmark_menu and not self._toc_menu else "CANCL", 8, 936, 1, BLACK, 52)
        canvas.text("USR-A", 72, 920, 1, BLACK, 52)
        canvas.text("BKMRK" if reader and not self._bookmark_menu and not self._toc_menu else "SLCT", 72, 936, 1, BLACK, 52)
        if reader and not self._bookmark_menu and not self._toc_menu:
            canvas.text("HOLD FOR LIST", 134, 936, 1, BLACK, 160)

    def _draw_book_state_icon(self, canvas, x, y, downloaded):
        if downloaded:
            # Closed book with a spine and page edges.
            canvas.rect(x + 16, y + 15, 31, 34, 3, BLACK)
            canvas.fill_rect(x + 22, y + 17, 2, 30, BLACK)
            canvas.fill_rect(x + 26, y + 39, 17, 2, BLACK)
            canvas.fill_rect(x + 26, y + 43, 17, 2, BLACK)
        else:
            # Pixel-aligned cloud outline for a remotely available book.
            for px, py, width, height in (
                (29, 16, 9, 3), (25, 19, 4, 4), (23, 23, 3, 8),
                (16, 26, 8, 3), (13, 29, 4, 4), (10, 33, 4, 10),
                (13, 43, 4, 4), (17, 47, 28, 3), (45, 43, 4, 4),
                (49, 34, 4, 10), (46, 30, 4, 5), (41, 27, 6, 3),
                (38, 19, 4, 9),
            ):
                canvas.fill_rect(x + px, y + py, width, height, BLACK)

    def _draw_book_border(self, canvas, index, selected, clear=False):
        y = LIBRARY_ROW_Y + index * LIBRARY_ROW_HEIGHT
        if clear:
            canvas.rect(31, y - 1, PORTRAIT_WIDTH - 62, 86, 5, WHITE)
        canvas.rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT, 2, BLACK)
        if selected:
            canvas.rect(31, y - 1, PORTRAIT_WIDTH - 62, 86, 1, BLACK)
            canvas.rect(34, y + 2, PORTRAIT_WIDTH - 68, 80, 2, BLACK)

    def _draw_footer_border(self, canvas, choice, selected):
        label, x = (("REFRESH LIST", 36), ("SETTINGS", 260))[choice]
        y = LIBRARY_ROW_Y + REFRESH_ROW * LIBRARY_ROW_HEIGHT
        canvas.rect(x - 4, y, HACK16.measure(label) + 8, 34, 2, BLACK if selected else WHITE)

    def _draw_library(self, canvas):
        self._draw_navigation_hints(canvas)
        canvas.wordmark(252, 16)
        self._draw_battery(canvas)
        canvas.fill_rect(32, 84, PORTRAIT_WIDTH - 64, 4, RED)

        # Text origins stay on 8-pixel boundaries so the pre-reversed bitmap
        # glyphs align with the optimized driver's native framebuffer bytes.
        row_y = LIBRARY_ROW_Y
        row_height = LIBRARY_ROW_HEIGHT
        progress_state = book_library.load_progress()
        recent = book_library.most_recent_read(self.all_books)
        if recent is not None:
            recent -= self.library_page * REFRESH_ROW
        for index, entry in enumerate(self.books):
            title = entry.get("title", "Untitled")
            author = entry.get("author", "Unknown Author")
            installed = "package" in entry and book_library.has_download(entry)
            saved = progress_state.get(entry.get("id"), {})
            word_count = max(1, int(entry.get("word_count", 1)))
            percent = min(100, int(int(saved.get("word", 0)) * 100 / word_count))
            progress = "{}%".format(percent)
            y = row_y + index * row_height
            self._draw_book_border(canvas, index, index == self.selected and not self._remove_prompt)
            # The icon tile alone has softly rounded corners.
            tile_y = y + 11
            canvas.fill_rect(46, tile_y, 54, 2, BLACK)
            canvas.fill_rect(46, tile_y + 60, 54, 2, BLACK)
            canvas.fill_rect(42, tile_y + 4, 2, 54, BLACK)
            canvas.fill_rect(102, tile_y + 4, 2, 54, BLACK)
            for dx, dy in ((2, 2), (58, 2), (2, 58), (58, 58)):
                canvas.fill_rect(42 + dx, tile_y + dy, 2, 2, BLACK)
            self._draw_book_state_icon(canvas, 42, tile_y, installed)
            canvas.text(title, 118, y + 8, 2, BLACK, 490)
            canvas.text(author, 118, y + 48, 1, BLACK, 340 if index == recent else 380)
            canvas.text(progress, 470 if index == recent else 522, y + 48, 1, RED, 54)
            if index == recent:
                canvas.text("RECENT", 536, y + 48, 1, BLACK, 72)

        y = row_y + REFRESH_ROW * row_height
        labels = (("REFRESH LIST", 36), ("SETTINGS", 260))
        for index, (label, x) in enumerate(labels):
            if self.selected == REFRESH_ROW and index == self._bottom_choice:
                self._draw_footer_border(canvas, index, True)
            canvas.text(label, x, y + 8, 1, BLACK, 210)

        if self._remove_prompt:
            canvas.fill_rect(75, 350, 530, 160, WHITE)
            canvas.rect(75, 350, 530, 160, 3, BLACK)
            canvas.text("REMOVE DOWNLOAD?", 100, 376, 2, BLACK, 480)
            canvas.text("[ YES ]" if self._remove_yes else "  YES  ", 130, 440, 2, BLACK, 180)
            canvas.text("  NO  " if self._remove_yes else "[ NO ]", 375, 440, 2, BLACK, 180)

        self._draw_bottom_hints(canvas)
        page_count = max(1, (len(self.all_books) + REFRESH_ROW - 1) // REFRESH_ROW)
        page_label = "PAGE {}/{}".format(self.library_page + 1, page_count)
        page_x = PORTRAIT_WIDTH - 32 - HACK16.measure(page_label)
        canvas.text(page_label, page_x, y + 8, 1, BLACK, PORTRAIT_WIDTH - 64)
        if self.update_status:
            canvas.text(self.update_status, 134, 912, 1, RED, PORTRAIT_WIDTH - 140)
        if page_count > 1:
            canvas.text("HOLD UP/DOWN FOR MORE BOOKS", 134, 936, 1, BLACK,
                        PORTRAIT_WIDTH - 140)
        if not self._remove_prompt and not self._bottom_menu:
            self._drawn_library_selection = self.selected
            self._drawn_library_choice = self._bottom_choice

    def _draw_settings(self, canvas):
        self._draw_navigation_hints(canvas)
        title = "TURN OFF LIGHTS" if self._sleep_options_menu else ("TEXT SIZE" if self._font_options_menu else ("FONT" if self._family_options_menu else ("LED SETTINGS" if getattr(self, '_led_menu', False) else "SETTINGS")))
        canvas.text(title, 240, 24, 2, BLACK, 300)
        self._draw_battery(canvas)
        canvas.fill_rect(32, 84, PORTRAIT_WIDTH - 64, 4, RED)
        if self._sleep_options_menu:
            for index, label in enumerate(("1 MINUTE", "5 MINUTES", "10 MINUTES", "NEVER")):
                y = LIBRARY_ROW_Y + index * LIBRARY_ROW_HEIGHT
                canvas.rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT, 3 if self._sleep_cursor == index else 1, BLACK)
                canvas.text(label, 52, y + 24, 2, BLACK, 350)
                if LIGHT_SLEEP_OPTIONS[index] == self._idle_light_minutes:
                    canvas.text("SELECTED", 480, y + 24, 1, BLACK, 120)
            self._draw_bottom_hints(canvas)
            return
        if self._font_options_menu:
            family = READER_FAMILIES[book_library.font_family()]
            sizes = tuple((label, family[size][0], size) for label, size in
                          (("Small", "small"), ("Medium", "medium"), ("Large", "large"), ("XLarge", "xl")))
            for index, (label, font, size) in enumerate(sizes):
                y = LIBRARY_ROW_Y + index * LIBRARY_ROW_HEIGHT
                canvas.rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT, 3 if self._font_cursor == index else 1, BLACK)
                canvas.text(label, 52, y if size == "xl" else y + 8, 1, BLACK, 300, font)
                if size == book_library.text_size():
                    canvas.text("SELECTED", 480, y + 24, 1, BLACK, 120)
            self._draw_bottom_hints(canvas)
            return
        if self._family_options_menu:
            for index, (label, family) in enumerate((('OpenDyslexic', 'opendyslexic'), ('Ubuntu', 'ubuntu'), ('Tinos', 'tinos'))):
                y = LIBRARY_ROW_Y + index * LIBRARY_ROW_HEIGHT
                canvas.rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT, 3 if self._family_cursor == index else 1, BLACK)
                canvas.text(label, 52, y + 8, 1, BLACK, 390, READER_FAMILIES[family]['medium'][0])
                if family == book_library.font_family():
                    canvas.text('SELECTED', 480, y + 24, 1, BLACK, 120)
            self._draw_bottom_hints(canvas)
            canvas.text('FONTS VIA NERDFONTS.COM', 390, 936, 1, BLACK, 280)
            return
        for index in range(4 if getattr(self, '_led_menu', False) else 8):
            self._draw_settings_row(canvas, index)
        if self.update_status:
            canvas.text(self.update_status, 134, 880, 1, RED, 500)
        self._draw_bottom_hints(canvas)
        credit = "FONTS VIA NERDFONTS.COM"
        canvas.text(credit, PORTRAIT_WIDTH - 2 - HACK16.measure(credit), 936, 1, BLACK, 260)

    def _draw_settings_row(self, canvas, index):
        row = index
        led_menu = getattr(self, '_led_menu', False)
        if not led_menu and row == 7:
            y = LIBRARY_ROW_Y + row * LIBRARY_ROW_HEIGHT
            canvas.rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT, 3 if self._settings_cursor == row else 1, BLACK)
            timeout = 'NEVER' if not self._sleep_timer_minutes else '{} MIN'.format(self._sleep_timer_minutes)
            canvas.text('SLEEP TIMER: ' + timeout, 48, y + 8, 2, BLACK, 570)
            canvas.text('Sleep after inactivity', 48, y + 40, 1, BLACK, 570)
            return
        if led_menu:
            index += 2
        timeout = "NEVER" if not self._idle_light_minutes else "{} MIN".format(self._idle_light_minutes)
        labels = ("TEXT SIZE: " + book_library.text_size().upper(), "FONT", "LED BRIGHTNESS: " + self._light_brightness.upper(),
                  "AVAILABILITY LIGHTS: " + ("ON" if self._availability_lights else "OFF"),
                  "Use LEDs for progress?", "TURN OFF LIGHTS: " + timeout, "WI-FI",
                  "QUICK BOOT: " + ("ON" if self._quick_boot_enabled else "OFF"))
        descriptions = ("", "", "Adjust indicator brightness", "Show colors for filled and empty slots",
                        "", "Turn off library lights when idle", "Manage Wi-Fi access",
                        "Skip red clear; may leave ghosts")
        label = labels[index]
        y = LIBRARY_ROW_Y + row * LIBRARY_ROW_HEIGHT
        canvas.rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT, 3 if self._settings_cursor == row else 1, BLACK)
        if not led_menu and row == 3:
            order, _ = book_library.library_sort_settings()
            canvas.text('LIBRARY SORT', 48, y + 8, 2, BLACK, 570)
            choices = (('TITLE', 'title', 48), ('AUTHOR FIRST', 'author_first', 108),
                       ('AUTHOR LAST', 'author_last', 238), ('DATE ADDED', 'added', 358),
                       ('RECENTLY OPENED', 'opened', 468))
            for text, value, x in choices:
                canvas.text(text, x, y + 40, 1, BLACK, 180)
                if value == order:
                    canvas.fill_rect(x, y + 66, HACK16.measure(text), 2, BLACK)
            if self._settings_inline == 'sort':
                x, width = ((44, 60), (104, 130), (234, 120), (354, 110), (464, 168))[self._sort_cursor]
                canvas.rect(x, y + 32, width, 48, 2, BLACK)
            return
        if not led_menu and row == 4:
            _, grouped = book_library.library_sort_settings()
            canvas.text('GROUP BY SERIES', 48, y + 8, 2, BLACK, 570)
            canvas.text('[X] Group books by series' if grouped else '[ ] Group books by series', 48, y + 40, 1, BLACK, 570)
            return
        if not led_menu and row in (2, 5, 6):
            main_labels = {2: 'LED SETTINGS...', 5: 'WI-FI & INTERNET SETTINGS...', 6: 'QUICK BOOT: ' + ('ON' if self._quick_boot_enabled else 'OFF')}
            main_descriptions = {2: 'Brightness and progress indicators', 5: 'Manage Wi-Fi access', 6: 'Skip red clear; may leave ghosts'}
            canvas.text(main_labels[row], 48, y + 8, 2, BLACK, 570)
            canvas.text(main_descriptions[row], 48, y + 40, 1, BLACK, 570)
            return
        if index == 0:
            canvas.text("SIZE", 40, y + 8, 2, BLACK, 70)
            family = READER_FAMILIES[book_library.font_family()]
            previews = (("Small", family['small'][0], 112, 24, 24, "small"),
                        ("Medium", family['medium'][0], 180, 16, 31, "medium"),
                        ("Large", family['large'][0], 300, 16, 46, "large"),
                        ("XLarge", family['xl'][0], 420, 0, 65, "xl"))
            for text, font, x, offset, underline, size in previews:
                canvas.text(text, x, y + offset, 1, BLACK, 200, font)
                if size == book_library.text_size():
                    canvas.fill_rect(x, y + offset + underline + 2, font.measure(text), 2, BLACK)
        elif index == 1:
            canvas.text('FONT', 40, y + 8, 2, BLACK, 100)
            for text, family, x in (('OpenDyslexic', 'opendyslexic', 140), ('Ubuntu', 'ubuntu', 350), ('Tinos', 'tinos', 490)):
                font = READER_FAMILIES[family]['medium'][0]
                canvas.text(text, x, y + 32, 1, BLACK, 200, font)
                if family == book_library.font_family():
                    canvas.fill_rect(x, y + 32 + font.line_height, font.measure(text), 2, BLACK)
        elif index == 4:
            canvas.text(label, 48, y + 8, 1, BLACK, 570)
            for text, mode, x in (("CHAPTER", "chapter", 48), ("BOOK", "book", 220), ("OFF", "off", 350)):
                canvas.text(text, x, y + 40, 1, BLACK, 150)
                if self._reading_light_mode == mode:
                    canvas.fill_rect(x, y + 66, HACK16.measure(text), 2, BLACK)
        else:
            canvas.text(label, 48, y + 8, 1, BLACK, 570)
            canvas.text(descriptions[index], 48, y + 40, 1, BLACK, 570)
        if self._settings_inline == 'size' and index == 0:
            x, width = ((106, 70), (176, 120), (296, 120), (416, 216))[self._font_cursor]
            canvas.rect(x, y, width, LIBRARY_FRAME_HEIGHT, 2, BLACK)
        elif self._settings_inline == 'family' and index == 1:
            x, width = ((136, 206), (346, 138), (486, 146))[self._family_cursor]
            canvas.rect(x, y + 32, width, 48, 2, BLACK)

    def _draw_detail(self, canvas):
        if not self.reader:
            canvas.text("NO BOOK OPEN", 32, 32, 2, RED)
            return
        entry = self.reader.entry
        lines, self.current_word = self.reader.read_page(self.current_page)
        title = entry.get("title", "Untitled")
        self._draw_navigation_hints(canvas, reader=True)
        if any(self._page_for_word(word) == self.current_page for word in self._bookmarks):
            # Filled ribbon with a white V cut into the lower edge.
            canvas.fill_rect(136, 5, 22, 37, BLACK)
            for row in range(11):
                canvas.fill_rect(146 - row, 31 + row, 2 + 2 * row, 1, WHITE)
        canvas.text(title, max(184, 594 - HACK16.measure(title)), 8, 1, BLACK, 410)
        chapter_title = ''
        for position, name in self._chapters:
            if int(position) > int(self.current_word):
                break
            chapter_title = str(name)
        if chapter_title:
            canvas.text(chapter_title, max(184, 594 - HACK16.measure(chapter_title)), 40, 1, BLACK, 410)
        self._draw_battery(canvas)
        canvas.fill_rect(32, 72, PORTRAIT_WIDTH - 64, 3, RED)
        size = self.reader.size or book_library.text_size()
        if self.reader.header.get("schema") == 1:
            size = self.reader.header.get("text_size", "medium")
            if size == "large":
                size = "xl"
        family = self.reader.font_family if self.reader.header.get('schema') != 1 else 'opendyslexic'
        reader_font, line_spacing, line_count = READER_FAMILIES[family][size]
        for index, line in enumerate(lines[:line_count]):
            canvas.text(
                line,
                32,
                80 + index * line_spacing,
                2,
                BLACK,
                PORTRAIT_WIDTH - 64,
                reader_font,
            )
        self._draw_bottom_hints(canvas, reader=True)
        canvas.text("{} / {}".format(self.current_page + 1, len(self.reader.pages)), 520, 928, 1, BLACK, 120)
        if self.update_status:
            canvas.text(self.update_status, 170, 888, 1, BLACK, 440)

    def _draw_bookmarks(self, canvas):
        self._draw_navigation_hints(canvas)
        canvas.text("BOOKMARKS", 250, 24, 2, BLACK, 300)
        self._draw_battery(canvas)
        canvas.fill_rect(32, 72, PORTRAIT_WIDTH - 64, 3, BLACK)
        if not self._bookmarks:
            canvas.text("NO BOOKMARKS YET", 32, 120, 2, BLACK, 500)
        else:
            start = (self._bookmark_cursor // 8) * 8
            total_words = max(1, int(self.reader.header.get("word_count", 1)))
            for row, word in enumerate(self._bookmarks[start:start + 8]):
                index = start + row
                y = 96 + row * 96
                canvas.rect(32, y, PORTRAIT_WIDTH - 64, 84, 3 if index == self._bookmark_cursor else 1, BLACK)
                canvas.text("BOOKMARK {}".format(index + 1), 54, y + 16, 2, BLACK, 410)
                canvas.text("{}%".format(min(100, int(word * 100 / total_words))), 530, y + 24, 1, BLACK, 100)
        self._draw_bottom_hints(canvas, reader=True)

    def _draw_toc(self, canvas):
        self._draw_navigation_hints(canvas)
        canvas.text("CHAPTERS", 240, 24, 2, BLACK, 300)
        self._draw_battery(canvas)
        canvas.fill_rect(32, 72, PORTRAIT_WIDTH - 64, 3, BLACK)
        if not self._chapters:
            canvas.text("NO CHAPTER INDEX", 32, 120, 2, BLACK, 550)
            canvas.text("RECONVERT THE BOOK TO ADD ONE", 32, 168, 1, BLACK, 580)
        else:
            start = (self._toc_cursor // 8) * 8
            for row, chapter in enumerate(self._chapters[start:start + 8]):
                index = start + row
                y = LIBRARY_ROW_Y + row * LIBRARY_ROW_HEIGHT
                canvas.rect(32, y, PORTRAIT_WIDTH - 64, LIBRARY_FRAME_HEIGHT,
                            3 if index == self._toc_cursor else 1, BLACK)
                canvas.text(chapter[1], 54, y + 24, 2, BLACK, 550)
        self._draw_bottom_hints(canvas, reader=True)

    def _draw_wifi_menu(self, canvas):
        self._draw_navigation_hints(canvas)
        title = "WIFI: " + self._wifi_chosen if self._wifi_view == "duration" else "WI-FI & INTERNET SETTINGS"
        canvas.text(title, 180, 24, 2, BLACK, 405)
        self._draw_battery(canvas)
        canvas.fill_rect(32, 72, PORTRAIT_WIDTH - 64, 3, BLACK)
        if self._wifi_view == "login":
            address, web_error = wifi_setup.network_portal_status()
            canvas.text("WEB LOGIN", 32, 120, 2, BLACK, 550)
            canvas.text("OPEN ON THE SAME NETWORK:", 32, 200, 1, BLACK, 590)
            canvas.text("http://" + address if address else "WIFI NOT CONNECTED", 32, 240, 2, BLACK, 590)
            hostname = wifi_setup.local_hostname()
            if hostname:
                canvas.text("http://" + hostname + ".local", 32, 296, 1, BLACK, 590)
            canvas.text("USER: pocketreader", 32, 360, 1, BLACK, 590)
            password = wifi_setup.web_password() if wifi_setup.show_device_passwords() else "HIDDEN"
            canvas.text("PASSWORD: " + password, 32, 400, 1, BLACK, 590)
            if web_error:
                canvas.wrapped_text("WEB ERROR: " + web_error, 32, 456, 590, 1, BLACK, line_gap=0)
            canvas.text("MENU+ TO RETURN", 32, 520, 1, BLACK, 590)
            self._draw_bottom_hints(canvas)
            return
        if self._wifi_view == "duration":
            items = list(WIFI_DURATION_ITEMS)
            if wifi_setup.radio_status()[0]:
                items.append("TURN WIFI OFF")
        else:
            connected = wifi_setup.radio_status()[1]
            preferred = wifi_setup.preferred_ssid()
            items = []
            for item in self._wifi_main_items():
                if item is None:
                    items.append(None)
                elif item[0] == 'hotspot':
                    items.append('SETUP HOTSPOT / ADD NETWORK')
                elif item[0] == 'starter':
                    items.append('STARTER LIBRARY: ' + ('ON' if starter_library.enabled() else 'OFF'))
                elif item[0] == 'login':
                    items.append('WEB LOGIN INFO')
                else:
                    name = item[1].get('ssid', '')
                    items.append(name + (' (ON)' if connected and name == preferred else ''))
        if not items:
            canvas.text("NO SAVED NETWORKS", 32, 120, 2, BLACK, 500)
            canvas.text("ADD ONE USING THE HOTSPOT", 32, 168, 1, BLACK, 550)
        else:
            start = (self._wifi_cursor // 8) * 8
            for row, label in enumerate(items[start:start + 8]):
                if label is None:
                    continue
                index = start + row
                y = 96 + row * 96
                canvas.rect(32, y, PORTRAIT_WIDTH - 64, 84, 3 if index == self._wifi_cursor else 1, BLACK)
                if self._wifi_view == 'main' and index == 7:
                    canvas.text(label, 54, y + 8, 2, BLACK, 550)
                    address, _ = wifi_setup.network_portal_status()
                    canvas.text('IP: ' + address if address else 'NOT CONNECTED', 54, y + 48, 1, BLACK, 550)
                else:
                    canvas.text(label, 54, y + 24, 2, BLACK, 550)
        if self._wifi_view == "main":
            address, web_error = wifi_setup.network_portal_status()
            if address and len(items) <= 7:
                canvas.text("IP: " + address, 48, 824, 1, BLACK, 590)
                hostname = wifi_setup.local_hostname()
                if hostname:
                    canvas.text("http://" + hostname + ".local", 48, 848, 1, BLACK, 590)
                if web_error:
                    canvas.text("WEB ERROR: " + web_error, 48, 872, 1, BLACK, 590)
                elif not self.update_status:
                    hint = "RESTART IF .LOCAL NAME FAILS" if wifi_setup.hostname_restart_required() else "CHANGE NAME IN WEB INTERFACE"
                    canvas.text(hint, 48, 872, 1, BLACK, 590)
            elif self._wifi_networks:
                canvas.text("HOLD USER A TO REMOVE", 170, 864, 1, BLACK, 440)
        if self._wifi_view == "delete":
            canvas.fill_rect(75, 350, 530, 160, WHITE)
            canvas.rect(75, 350, 530, 160, 3, BLACK)
            canvas.text("REMOVE NETWORK?", 100, 376, 2, BLACK, 480)
            canvas.text("[ YES ]" if self._wifi_delete_yes else "  YES  ", 130, 440, 2, BLACK, 180)
            canvas.text("  NO  " if self._wifi_delete_yes else "[ NO ]", 375, 440, 2, BLACK, 180)
        if self.update_status.startswith("WIFI ERROR:"):
            canvas.fill_rect(48, 320, PORTRAIT_WIDTH - 96, 208, WHITE)
            canvas.rect(48, 320, PORTRAIT_WIDTH - 96, 208, 2, BLACK)
            canvas.wrapped_text(self.update_status, 64, 336, PORTRAIT_WIDTH - 128,
                                1, BLACK, line_gap=0)
        elif self.update_status:
            canvas.text(self.update_status, 170, 888, 1, BLACK, 440)
        self._draw_bottom_hints(canvas)

    def _draw_setup(self, canvas):
        ssid, password = wifi_setup.credentials()
        canvas.wordmark(30, 8)
        canvas.fill_rect(32, 88, PORTRAIT_WIDTH - 64, 4, RED)
        canvas.text("WIFI SETUP", 32, 120, 2, RED)
        canvas.wrapped_text(
            "Connect a phone or computer to this temporary network:",
            32,
            176,
            PORTRAIT_WIDTH - 64,
            2,
            BLACK,
            8,
            OPEN_DYSLEXIC24,
        )
        canvas.text(ssid, 32, 304, 2, BLACK, PORTRAIT_WIDTH - 64)
        canvas.text("PASSWORD", 32, 368, 1, RED)
        canvas.text(password if wifi_setup.show_device_passwords() else "HIDDEN", 32, 400, 2, BLACK, PORTRAIT_WIDTH - 64)
        canvas.text("THEN OPEN", 32, 480, 1, RED)
        canvas.text("http://192.168.4.1", 32, 512, 2, BLACK, PORTRAIT_WIDTH - 64)
        canvas.text("The hotspot password grants access.", 32, 552, 1, BLACK, PORTRAIT_WIDTH - 64)
        canvas.text("No separate web login is needed here.", 32, 576, 1, BLACK, PORTRAIT_WIDTH - 64)
        canvas.text("On regular Wi-Fi, use the web login.", 32, 600, 1, BLACK, PORTRAIT_WIDTH - 64)
        canvas.wrapped_text(
            "Press MENU+ to close the hotspot. It closes after ten minutes unless in use.",
            32,
            640,
            PORTRAIT_WIDTH - 64,
            2,
            BLACK,
            8,
            OPEN_DYSLEXIC24,
        )
