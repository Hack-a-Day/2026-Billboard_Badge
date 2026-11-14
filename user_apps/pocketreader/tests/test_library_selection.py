"""Check fast library-border redraws and their fallback while the panel is busy."""
import ast
from pathlib import Path
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'overlay/apps/pocketreader.py'

class Canvas:
    def __init__(self, *_): self.pixels = set()
    def rect(self, x, y, w, h, thickness, color):
        for yy in range(y, y+h):
            for xx in range(x, x+w):
                if xx < x+thickness or xx >= x+w-thickness or yy < y+thickness or yy >= y+h-thickness:
                    if color: self.pixels.add((xx, yy))
                    else: self.pixels.discard((xx, yy))

class LibrarySelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(SOURCE.read_text())
        app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PocketReaderApp')
        methods = [n for n in app.body if isinstance(n, ast.FunctionDef) and n.name in ('_move_selection', '_draw_book_border', '_draw_footer_border', '_update_library_border', '_show_library_page', '_move_library_page')]
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
        cls.scope = {'REFRESH_ROW': 8, 'PORTRAIT_WIDTH': 672, 'BLACK': 1, 'WHITE': 0,
                     'LIBRARY_ROW_Y': 96, 'LIBRARY_ROW_HEIGHT': 96, 'LIBRARY_FRAME_HEIGHT': 84,
                     'HACK16': types.SimpleNamespace(measure=lambda text: len(text) * 8),
                     'display': types.SimpleNamespace(black=None, red=None)}
        exec(compile(code, str(SOURCE), 'exec'), cls.scope)

    def test_moving_selection_dismisses_error_with_full_refresh(self):
        app=self.scope['App']();app.books=[{},{}];app.selected=0;app._bottom_choice=0
        app.update_status='BOOK ERROR: detailed explanation';app._show_selection_led=lambda:None
        app._move_selection(1)
        self.assertEqual(app.update_status,'')
        self.assertEqual(app.selected,1)
        self.assertTrue(app._dirty)
        self.assertFalse(app._small_update)

    def test_incremental_border_matches_full_draw(self):
        app = self.scope['App'](); initial, expected = Canvas(), Canvas()
        for index in range(7):
            app._draw_book_border(initial, index, index == 2)
            app._draw_book_border(expected, index, index == 3)
        app._draw_book_border(initial, 2, False, clear=True)
        app._draw_book_border(initial, 3, True, clear=True)
        self.assertEqual(initial.pixels, expected.pixels)

    def test_bottom_actions_are_direct_navigation_stops(self):
        app = self.scope['App'](); app.books = [{} for _ in range(8)]
        app.selected = 7; app._bottom_choice = 0; app._dirty = True
        app._drawn_library_selection = None; app._selection_update_pending = False
        app._show_selection_led = lambda: None
        app._move_selection(1); self.assertEqual((app.selected, app._bottom_choice), (8, 0))
        app._move_selection(1); self.assertEqual((app.selected, app._bottom_choice), (8, 1))
        app._move_selection(-1); self.assertEqual((app.selected, app._bottom_choice), (8, 0))
        app._move_selection(-1); self.assertEqual((app.selected, app._bottom_choice), (7, 0))

    def test_page_change_requests_full_refresh_immediately(self):
        app = self.scope['App'](); app.all_books = [{} for _ in range(9)]
        app.library_page = 0; app._show_selection_led = lambda: None
        app._move_library_page(1)
        self.assertEqual(app.library_page, 1)
        self.assertEqual(len(app.books), 1)
        self.assertTrue(app._page_update_pending)
        self.assertTrue(app._dirty)
        self.assertFalse(app._small_update)

    def test_queues_partial_or_falls_back_when_busy(self):
        app = self.scope['App'](); canvas = Canvas(); queued = []
        self.scope['PortraitCanvas'] = lambda *_: canvas
        app.books = [{}, {}, {}]; app._bottom_choice = 0; app.selected = 0; app._dirty = False; app._drawn_library_selection = 0; app._drawn_library_choice = 0; app._selection_update_pending = False
        app.reader_display = types.SimpleNamespace(ready=lambda: True, queue_frame=lambda partial: queued.append(partial))
        app._show_selection_led = lambda: None
        app._move_selection(1)
        self.assertEqual(queued, [True]); self.assertFalse(app._dirty); self.assertEqual(app._drawn_library_selection, 1)
        app.reader_display.ready = lambda: False
        app._move_selection(1)
        self.assertEqual(app.selected, 2)
        self.assertFalse(app._dirty)
        self.assertTrue(app._selection_update_pending)
        self.assertEqual(app._drawn_library_selection, 1)
        app.reader_display.ready = lambda: True
        self.assertTrue(app._update_library_border())
        self.assertEqual(app._drawn_library_selection, 2)
        self.assertEqual(queued, [True, True])

    def test_footer_moves_update_only_borders_and_coalesce(self):
        app = self.scope['App'](); canvas = Canvas(); queued = []
        self.scope['PortraitCanvas'] = lambda *_: canvas
        app.books = [{} for _ in range(8)]; app.selected = 7; app._bottom_choice = 0
        app._drawn_library_selection = 7; app._drawn_library_choice = 0
        app._dirty = False; app._selection_update_pending = False
        app._show_selection_led = lambda: None
        app.reader_display = types.SimpleNamespace(ready=lambda: True, queue_frame=lambda partial: queued.append(partial))
        app._draw_book_border(canvas, 7, True)
        app._move_selection(1)
        self.assertEqual((app._drawn_library_selection, app._drawn_library_choice), (8, 0))
        self.assertEqual(queued, [True]); self.assertFalse(app._dirty)
        expected = Canvas(); app._draw_book_border(expected, 7, False); app._draw_footer_border(expected, 0, True)
        self.assertEqual(canvas.pixels, expected.pixels)
        app.reader_display.ready = lambda: False
        app._move_selection(1)
        self.assertTrue(app._selection_update_pending)
        app.reader_display.ready = lambda: True
        self.assertTrue(app._update_library_border())
        expected = Canvas(); app._draw_book_border(expected, 7, False); app._draw_footer_border(expected, 1, True)
        self.assertEqual(canvas.pixels, expected.pixels)
        self.assertEqual(queued, [True, True])
        app._move_selection(1)
        self.assertEqual(app._drawn_library_selection, 0)
        self.assertEqual(queued, [True, True, True])
