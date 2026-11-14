"""Library LED sleep responds to inactivity and wakes on button input."""
import ast
from pathlib import Path
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'overlay/apps/pocketreader.py'

class LibraryIdle(unittest.TestCase):
    def test_settings_change_sizes_and_idle_preference(self):
        tree = ast.parse(SOURCE.read_text())
        app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PocketReaderApp')
        methods = [n for n in app.body if isinstance(n, ast.FunctionDef) and n.name == '_run_settings_menu']
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
        class Button:
            state = False
            def pressed(self):
                value = self.state; self.state = False; return value
        back, up, down, select = Button(), Button(), Button(), Button()
        values = {}
        library = types.SimpleNamespace(set_text_size=lambda value: values.update(size=value),
                    set_idle_light_minutes=lambda value: values.update(idle=value), set_reading_light_mode=lambda value: values.update(progress=value), set_quick_boot_enabled=lambda value: values.update(quick_boot=value), text_size=lambda: 'medium', font_family=lambda: 'opendyslexic', set_font_family=lambda value: values.update(font=value), set_light_setting=lambda key, value: values.update({key: value}))
        scope = {'PhysicalMenuPlus': back, 'PhysicalMenuMinus': up, 'UserB': down, 'UserA': select, 'book_library': library, 'LIGHT_SLEEP_OPTIONS': (1,5,10,0)}
        exec(compile(code, str(SOURCE), 'exec'), scope)
        instance = scope['App'](); instance._settings_menu = True; instance._settings_cursor = 0
        instance._suppress_chord = False; instance._toc_menu = False; instance._bookmark_menu = False; instance.detail = False
        instance._family_options_menu = False; instance._family_cursor = 0
        instance._settings_inline = None; instance._settings_row_pending = 0
        instance._queue_settings_rows = lambda *rows: None; instance._flush_settings_rows = lambda: False
        instance._menu_update_pending = False; instance._menu_selection_changed = lambda: None
        instance._reading_light_mode = 'off'; instance._idle_light_minutes = 5; instance._sleep_options_menu = False; instance._sleep_cursor = 0; instance._font_options_menu = False; instance._font_cursor = 0; instance._light_brightness = 'medium'; instance._availability_lights = False; instance._dirty = False; instance._small_update = False
        instance._show_settings_led = lambda: None; instance._show_selection_led = lambda: None
        instance._select_first_on_library_page = lambda: None
        instance._draw = lambda: None; instance.reader_display = types.SimpleNamespace(ready=lambda: False)
        instance._quick_boot_enabled = False
        select.state = True; instance._run_settings_menu(); self.assertEqual(instance._settings_inline, 'size')
        select.state = True; instance._run_settings_menu(); self.assertEqual(values['size'], 'medium')
        down.state = True; instance._run_settings_menu(); self.assertEqual(instance._settings_cursor, 1)
        select.state = True; instance._run_settings_menu(); self.assertEqual(instance._settings_inline, 'family')
        down.state = True; instance._run_settings_menu(); select.state = True; instance._run_settings_menu()
        self.assertEqual(values['font'], 'ubuntu'); self.assertIsNone(instance._settings_inline)
        instance._settings_cursor = 2
        select.state = True; instance._run_settings_menu(); self.assertTrue(instance._led_menu)
        instance._settings_cursor = 3
        select.state = True; instance._run_settings_menu(); self.assertTrue(instance._sleep_options_menu)
        self.assertEqual(instance._sleep_cursor, 1)
        down.state = True; instance._run_settings_menu(); self.assertEqual(instance._sleep_cursor, 2)
        select.state = True; instance._run_settings_menu(); self.assertEqual(values['idle'], 10)
        self.assertFalse(instance._sleep_options_menu)
        select.state = True; instance._run_settings_menu(); down.state = True; instance._run_settings_menu()
        select.state = True; instance._run_settings_menu(); self.assertEqual(values['idle'], 0)
        instance._settings_cursor = 0; select.state = True; instance._run_settings_menu(); self.assertEqual(values['light_brightness'], 'bright')
        instance._settings_cursor = 1; select.state = True; instance._run_settings_menu(); self.assertTrue(values['availability_lights'])
        instance._settings_cursor = 2; select.state = True; instance._run_settings_menu(); self.assertEqual(values['progress'], 'chapter')
        select.state = True; instance._run_settings_menu(); self.assertEqual(values['progress'], 'book')
        back.state = True; instance._run_settings_menu(); self.assertFalse(instance._led_menu)
        instance._settings_cursor = 6; select.state = True; instance._run_settings_menu()
        self.assertTrue(values['quick_boot']); self.assertTrue(instance.reader_display.quick_boot)
        down.state = True; instance._run_settings_menu(); self.assertEqual(instance._settings_cursor, 7)
        down.state = True; instance._run_settings_menu(); self.assertEqual(instance._settings_cursor, 0)
        instance._settings_cursor = 5; instance._begin_wifi_menu = lambda: values.update(wifi=True)
        select.state = True; instance._run_settings_menu(); self.assertTrue(values['wifi'])
        instance._settings_menu = True
        back.state = True; instance._run_settings_menu(); self.assertFalse(instance._settings_menu)
        self.assertFalse(instance._small_update)

    def test_settings_availability_lights_follow_visible_options(self):
        tree = ast.parse(SOURCE.read_text())
        app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PocketReaderApp')
        methods = [n for n in app.body if isinstance(n, ast.FunctionDef) and n.name in ('_show_settings_led', '_light_level')]
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
        class LEDs(dict):
            def pause(self): pass
            def fill(self, color):
                for index in range(8): self[index] = color
            def write(self): pass
        lights = LEDs()
        scope = {'leds': lights, 'ROW_LED_MAP': (7,6,5,4,3,2,1,0)}
        exec(compile(code, str(SOURCE), 'exec'), scope)
        instance = scope['App'](); instance._light_brightness = 'medium'; instance._availability_lights = True
        instance._settings_cursor = 2; instance._sleep_cursor = 1; instance._font_cursor = 3
        instance._family_cursor = 0; instance._family_options_menu = False
        instance._sleep_options_menu = instance._font_options_menu = False
        instance._show_settings_led()
        self.assertEqual(lights[5], (2,2,2)); self.assertEqual(lights[7], (0,0,2))
        self.assertEqual(lights[1], (0,0,2)); self.assertEqual(lights[0], (0,0,2))
        instance._led_menu = True; instance._show_settings_led()
        self.assertEqual(lights[4], (0,0,2)); self.assertEqual(lights[3], (0,0,0))
        instance._sleep_options_menu = True; instance._show_settings_led()
        self.assertEqual(lights[6], (2,2,2)); self.assertEqual(lights[4], (0,0,2))
        self.assertEqual(lights[3], (0,0,0))
        instance._sleep_options_menu = False; instance._font_options_menu = True; instance._show_settings_led()
        self.assertEqual(lights[4], (2,2,2)); self.assertEqual(lights[3], (0,0,0))
        instance._availability_lights = False; instance._show_settings_led()
        self.assertEqual(lights[7], (0,0,0)); self.assertEqual(lights[4], (2,2,2))
        instance._light_brightness = 'off'; instance._show_settings_led()
        self.assertTrue(all(color == (0,0,0) for color in lights.values()))

    def test_white_indicator_and_idle_wake(self):
        tree = ast.parse(SOURCE.read_text())
        app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PocketReaderApp')
        methods = [n for n in app.body if isinstance(n, ast.FunctionDef) and n.name in ('_show_selection_led', '_update_library_idle_lights', '_light_level')]
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
        class LEDs(dict):
            def pause(self): pass
            def fill(self, color):
                for i in range(8): self[i] = color
            def write(self): pass
        light = LEDs(); button = types.SimpleNamespace(value=False); clock = [0]
        scope = {'leds': light, 'ROW_LED_MAP': (7,6,5,4,3,2,1,0), 'REFRESH_ROW': 8, 
                 'time': types.SimpleNamespace(ticks_ms=lambda: clock[0], ticks_diff=lambda a,b: a-b),
                 'UserA': button, 'UserB': button, 'PhysicalMenuMinus': button, 'PhysicalMenuPlus': button}
        exec(compile(code, str(SOURCE), 'exec'), scope)
        instance = scope['App'](); instance.selected = 0; instance._remove_prompt = False; instance._light_brightness = 'medium'; instance._availability_lights = False; instance.books = [{}]
        instance._library_lights_asleep = False; instance._library_idle_since = 0; instance._idle_light_minutes = 1; instance._settings_menu = False
        instance.detail = instance._wifi_menu = instance._bottom_menu = instance._setup_pending = instance._exit_pending = False
        instance._download_task = instance._catalog_task = instance._wifi_task = None
        instance._show_selection_led(); self.assertEqual(light[7], (2,2,2)); self.assertEqual(light[6], (0,0,0))
        instance.selected = 8; instance._show_selection_led()
        self.assertTrue(all(color == (0,0,0) for color in light.values()))
        instance.selected = 0
        clock[0] = 59999; instance._update_library_idle_lights(); self.assertFalse(instance._library_lights_asleep)
        clock[0] = 60000; instance._update_library_idle_lights(); self.assertTrue(instance._library_lights_asleep); self.assertEqual(light[7], (0,0,0))
        button.value = True; instance._update_library_idle_lights(); self.assertFalse(instance._library_lights_asleep); self.assertEqual(light[7], (2,2,2))
        button.value = False
        for minutes in (5, 10):
            instance._idle_light_minutes = minutes; instance._library_idle_since = 0; instance._library_lights_asleep = False
            clock[0] = minutes * 60000 - 1; instance._update_library_idle_lights(); self.assertFalse(instance._library_lights_asleep)
            clock[0] += 1; instance._update_library_idle_lights(); self.assertTrue(instance._library_lights_asleep)
        instance._idle_light_minutes = 0; instance._library_lights_asleep = False; instance._library_idle_since = 0
        clock[0] = 3600000; instance._update_library_idle_lights(); self.assertFalse(instance._library_lights_asleep)
        instance._remove_prompt = True; instance._show_selection_led(); self.assertEqual(light[7], (2,0,0))
