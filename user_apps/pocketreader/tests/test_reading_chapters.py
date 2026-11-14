"""Chapter shortcut and progress lights with simulated buttons and LEDs."""
import ast
from pathlib import Path
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'overlay/apps/pocketreader.py'

class ReadingChapters(unittest.TestCase):
    def methods(self, names, scope):
        tree = ast.parse(SOURCE.read_text())
        app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PocketReaderApp')
        selected = [n for n in app.body if isinstance(n, ast.FunctionDef) and n.name in names]
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=selected, decorator_list=[])], type_ignores=[]))
        exec(compile(code, str(SOURCE), 'exec'), scope)
        return scope['App']()

    def test_hold_both_buttons_opens_toc_without_page_press(self):
        previous = types.SimpleNamespace(value=False, _pressed_state=0)
        next_button = types.SimpleNamespace(value=False, _pressed_state=0)
        clock = [0]
        scope = {'PhysicalMenuMinus': previous, 'UserB': next_button, 'PRESSED_READ': 2, 'EXIT_HOLD_MS': 3000,
                 'time': types.SimpleNamespace(ticks_ms=lambda: clock[0], ticks_diff=lambda a,b: a-b)}
        app = self.methods(('_handle_toc_chord',), scope)
        app.detail = True; app._bookmark_menu = False; app._toc_menu = False
        app.reader = types.SimpleNamespace(pages=[[0,0,0],[1,0,60]])
        app.current_page = 1; app._chapters = [[0,'First'],[50,'Second']]
        app._toc_chord_suppressed = False; app._toc_chord_started = None
        app._toc_hold_ready = False
        feedback = []
        app._update_reading_hold_feedback = lambda owner, elapsed: feedback.append((owner, elapsed))
        app._clear_reading_hold_feedback = lambda owner: None
        app._previous_tracking = app._sleep_button_tracking = False
        previous.value = True; self.assertFalse(app._handle_toc_chord())
        next_button.value = True; self.assertTrue(app._handle_toc_chord())
        clock[0] = 2999; app._handle_toc_chord(); self.assertFalse(app._toc_menu)
        clock[0] = 3000; app._handle_toc_chord(); self.assertFalse(app._toc_menu)
        self.assertEqual(feedback[-1], ('toc', 3000))
        previous.value = False; self.assertTrue(app._handle_toc_chord())
        self.assertTrue(app._toc_menu); self.assertEqual(app._toc_cursor, 1)
        next_button.value = False; self.assertTrue(app._handle_toc_chord())
        self.assertEqual((previous._pressed_state, next_button._pressed_state), (2,2))
        self.assertFalse(app._handle_toc_chord())

    def test_selecting_a_chapter_jumps_and_saves_its_page(self):
        class Button:
            value = False
            _pressed_state = 0
            def pressed_ms(self): return 0
        button = Button(); scope = {'UserA': button, 'EXIT_HOLD_MS': 3000, 'PRESSED_READ': 2}
        app = self.methods(('_handle_select_button', '_page_for_word'), scope)
        app.detail = True; app._toc_menu = True; app._bookmark_menu = False
        app._chapters = [[40, 'Chapter one'], [80, 'Chapter two']]; app._toc_cursor = 1
        app.reader = types.SimpleNamespace(pages=[[0,0,0],[1,0,25],[2,0,60],[3,0,85]])
        app.current_page = 0; app._suppress_select = False; app._select_tracking = False
        app._clear_reading_hold_feedback = lambda owner: None
        app._select_long_fired = False; saved = []; app._save_reader_progress = lambda: saved.append(app.current_page)
        button.value = True; app._handle_select_button()
        button.value = False; app._handle_select_button()
        self.assertEqual(app.current_page, 2); self.assertEqual(saved, [2]); self.assertFalse(app._toc_menu)

    def test_bookmark_list_opens_after_three_second_hold_is_released(self):
        class Button:
            value = False; elapsed = 0; _pressed_state = 0
            def pressed_ms(self): return self.elapsed
        button = Button(); feedback = []
        scope = {'UserA':button, 'EXIT_HOLD_MS':3000, 'PRESSED_READ':2,
                 'book_library':types.SimpleNamespace(bookmarks=lambda book_id:[12, 34])}
        app = self.methods(('_handle_select_button',), scope)
        app.detail = True; app._toc_menu = app._bookmark_menu = False
        app.reader = types.SimpleNamespace(entry={'id':'book'})
        app._suppress_select = app._select_tracking = app._select_long_fired = False
        app._bookmark_hold_ready = False
        app._update_reading_hold_feedback = lambda owner, elapsed: feedback.append((owner, elapsed))
        app._clear_reading_hold_feedback = lambda owner: None
        button.value = True; button.elapsed = 3000; app._handle_select_button()
        self.assertEqual(feedback[-1], ('bookmark', 3000)); self.assertFalse(app._bookmark_menu)
        button.value = False; app._handle_select_button()
        self.assertTrue(app._bookmark_menu); self.assertEqual(app._bookmarks, [12, 34])

    def test_reader_header_shows_current_chapter_without_losing_text_lines(self):
        class Font:
            def measure(self, text): return len(text) * 8
        class Canvas:
            def __init__(self): self.texts = []
            def text(self, text, x, y, *args):
                if y % 8: raise ValueError('Text must be aligned')
                self.texts.append((text, y))
            def fill_rect(self, *args): pass
        font = Font()
        scope = {'HACK16':font, 'BLACK':1, 'RED':2, 'PORTRAIT_WIDTH':672,
                 'book_library':types.SimpleNamespace(text_size=lambda: 'medium'),
                 'READER_FAMILIES':{'opendyslexic':{'medium':(font, 32, 24)}}}
        app = self.methods(('_draw_detail',), scope)
        app.reader = types.SimpleNamespace(entry={'title':'Book'}, size='medium', header={'schema':2},
                    font_family='opendyslexic', pages=[[0,0,0],[0,0,60]],
                    read_page=lambda page: (['Body line'] * 24, 60 if page else 0))
        app.current_page = 1; app._chapters = [[0,'Chapter One'], [50,'Chapter Two']]
        app._bookmarks = []; app.update_status = ''
        app._draw_navigation_hints = app._draw_battery = app._draw_bottom_hints = lambda *args, **kwargs: None
        canvas = Canvas(); app._draw_detail(canvas)
        self.assertIn(('Book', 8), canvas.texts)
        self.assertIn(('Chapter Two', 40), canvas.texts)
        self.assertEqual(len([text for text, _ in canvas.texts if text == 'Body line']), 24)
        app._chapters = [[100, 'Later']]
        canvas = Canvas(); app._draw_detail(canvas)
        self.assertNotIn(('Later', 40), canvas.texts)

    def test_book_and_chapter_progress_use_configured_brightness(self):
        class LEDs(dict):
            def pause(self): pass
            def fill(self, value):
                for index in range(8): self[index] = value
            def write(self): pass
        lights = LEDs(); scope = {'leds': lights, 'ROW_LED_MAP': (7,6,5,4,3,2,1,0)}
        app = self.methods(('_show_reading_progress', '_light_level'), scope)
        app._light_brightness = 'medium'; app._reading_light_mode = 'book'
        app.reader = types.SimpleNamespace(pages=[[0,0,0],[1,0,25],[2,0,50],[3,0,75]], header={'word_count': 100})
        app.current_page = 0; app._chapters = [[0,'First'],[50,'Second']]
        app._show_reading_progress(); self.assertEqual(sum(value != (0,0,0) for value in lights.values()), 2)
        app.current_page = 3; app._show_reading_progress(); self.assertEqual(sum(value != (0,0,0) for value in lights.values()), 8)
        app._reading_light_mode = 'chapter'; app.current_page = 2; app._show_reading_progress()
        self.assertEqual(sum(value != (0,0,0) for value in lights.values()), 4)
        app._chapters = []; app._show_reading_progress(); self.assertTrue(all(value == (0,0,0) for value in lights.values()))
        app._reading_light_mode = 'book'; app._light_brightness = 'off'; app._show_reading_progress()
        self.assertTrue(all(value == (0,0,0) for value in lights.values()))
