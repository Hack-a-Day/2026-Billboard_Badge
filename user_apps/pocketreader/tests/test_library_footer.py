"""Check that library actions and page number fit beneath eight book slots."""
import ast
from pathlib import Path
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'overlay/apps/pocketreader.py'

class LibraryFooter(unittest.TestCase):
    def test_page_number_and_actions_share_footer(self):
        tree = ast.parse(SOURCE.read_text())
        app_class = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == 'PocketReaderApp')
        method = next(item for item in app_class.body if isinstance(item, ast.FunctionDef) and item.name == '_draw_library')
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=[method], decorator_list=[])], type_ignores=[]))
        class Font:
            def measure(self, text): return len(text) * 12
        font = Font()
        class Canvas:
            def __init__(self): self.texts = []
            def text(self, *args):
                if args[2] % 8:
                    raise ValueError('bitmap text y must align to an 8-pixel boundary')
                self.texts.append(args)
            def rect(self, *args): pass
            def fill_rect(self, *args): pass
            def wordmark(self, *args): pass
        lib = types.SimpleNamespace(load_progress=lambda: {}, most_recent_read=lambda _: None,
                                    has_download=lambda _: True)
        scope = {'book_library': lib, 'PORTRAIT_WIDTH': 672, 'BLACK': 1, 'RED': 2,
                 'REFRESH_ROW': 8, 'LIBRARY_ROW_Y': 96, 'LIBRARY_ROW_HEIGHT': 96,
                 'OPEN_DYSLEXIC24': font, 'HACK16': font}
        exec(compile(code, str(SOURCE), 'exec'), scope)
        app = scope['App'](); app.books = []; app.all_books = [{} for _ in range(9)]
        app.library_page = 0; app.selected = 8; app._bottom_choice = 0
        app._remove_prompt = app._bottom_menu = False; app.update_status = 'LIBRARY UPDATED'
        app._draw_navigation_hints = app._draw_battery = app._draw_bottom_hints = lambda *_: None
        app._draw_footer_border = lambda *_: None
        canvas = Canvas(); app._draw_library(canvas)
        label = next(item for item in canvas.texts if item[0] == 'PAGE 1/2')
        self.assertEqual(label[1], 672 - 32 - font.measure(label[0]))
        self.assertEqual(label[2], 872)
        self.assertTrue(any(item[0] == 'REFRESH LIST' and item[2] == 872 for item in canvas.texts))
        self.assertTrue(any(item[0] == 'SETTINGS' and item[2] == 872 for item in canvas.texts))
        status = next(item for item in canvas.texts if item[0] == 'LIBRARY UPDATED')
        self.assertGreaterEqual(status[2], label[2] + 40)
        self.assertEqual(status[1], 134)
        self.assertTrue(any(item[0] == 'HOLD UP/DOWN FOR MORE BOOKS' for item in canvas.texts))
        app.books = [{'id': str(i), 'title': 'Book {}'.format(i), 'author': 'Author', 'package': 'book.prb'} for i in range(8)]
        app.all_books = app.books + [{'id': 'extra'}]
        app._draw_book_border = lambda *_: None
        app._draw_book_state_icon = lambda *_: None
        canvas = Canvas(); app._draw_library(canvas)
        self.assertEqual(len([item for item in canvas.texts if item[0].startswith('Book ')]), 8)
        self.assertTrue(any(item[0] == 'PAGE 1/2' for item in canvas.texts))
