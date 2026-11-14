"""The A+B shortcut opens settings without selecting an option on release."""
import ast
from pathlib import Path
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'overlay/apps/pocketreader.py'


class SettingsShortcut(unittest.TestCase):
    def test_chord_entry_and_button_release(self):
        tree = ast.parse(SOURCE.read_text())
        app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PocketReaderApp')
        methods = [n for n in app.body if isinstance(n, ast.FunctionDef) and n.name in ('_begin_settings', '_run_settings_menu')]
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
        a = types.SimpleNamespace(value=True, _pressed_state=0)
        b = types.SimpleNamespace(value=True, _pressed_state=0)
        scope = {'UserA': a, 'UserB': b, 'PRESSED_READ': 7}
        exec(compile(code, str(SOURCE), 'exec'), scope)
        instance = scope['App'](); instance._show_settings_led = lambda: None
        instance._begin_settings(from_chord=True)
        self.assertTrue(instance._settings_menu)
        self.assertEqual(instance._settings_cursor, 0)
        instance._run_settings_menu()
        self.assertFalse(instance._font_options_menu)
        a.value = b.value = False
        instance._run_settings_menu()
        self.assertFalse(instance._suppress_chord)
        self.assertEqual((a._pressed_state, b._pressed_state), (7, 7))
        self.assertFalse(instance._font_options_menu)
