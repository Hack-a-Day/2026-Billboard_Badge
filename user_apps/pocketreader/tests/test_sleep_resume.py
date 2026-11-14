"""Sleep restoration must not change a normal startup."""
import importlib.util
import ast
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class SleepResume(unittest.TestCase):
    def test_wake_requires_two_seconds_and_short_press_returns_to_sleep(self):
        for release_at in (None, 400):
            clock = {'ms': 0}; events = []
            fake_time = types.ModuleType('time')
            fake_time.ticks_ms = lambda: clock['ms']
            fake_time.ticks_diff = lambda a, b: a - b
            def sleep_ms(ms): clock['ms'] += ms
            fake_time.sleep_ms = sleep_ms
            class Pin:
                IN = 1; PULL_UP = 2
                def __init__(self, *args): pass
                def value(self): return int(release_at is not None and clock['ms'] >= release_at)
            machine = types.ModuleType('machine'); machine.Pin = Pin
            machine.deepsleep = lambda: events.append('sleep')
            esp = types.ModuleType('esp32'); esp.WAKEUP_ALL_LOW = 0
            esp.wake_on_ext0 = lambda pin, level: events.append(('arm', level))
            with patch.dict(sys.modules, {'machine': machine, 'esp32': esp, 'time': fake_time}):
                spec = importlib.util.spec_from_file_location('wake_hold_test', ROOT / 'overlay/lib/pocketreader_sleep.py')
                module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                result = module.confirm_wake_hold()
            if release_at is None:
                self.assertTrue(result); self.assertEqual(clock['ms'], 2000); self.assertEqual(events, [])
            else:
                self.assertFalse(result); self.assertEqual(events, [('arm', 0), 'sleep'])

    def test_open_book_restores_on_first_launch_after_restart(self):
        machine = types.ModuleType('machine'); machine.DEEPSLEEP_RESET = 4
        machine.reset_cause = lambda: 1
        with patch.dict(sys.modules, {'machine': machine}), tempfile.TemporaryDirectory() as tmp:
            spec = importlib.util.spec_from_file_location('power_resume_test', ROOT / 'overlay/lib/pocketreader_sleep.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            module.SESSION_PATH = str(Path(tmp) / 'open-book.json')
            module.save_open_book('book-a')
            # An unrelated app can run first without consuming this launch.
            self.assertEqual(module.take_first_launch()['book_id'], 'book-a')
            self.assertIsNone(module.take_first_launch())
            module._first_launch = True  # A subsequent power-on creates a new runtime.
            self.assertEqual(module.take_first_launch()['book_id'], 'book-a')
            Path(module.SESSION_PATH).rename(module.SESSION_PATH + '.previous')
            module._first_launch = True
            self.assertEqual(module.take_first_launch()['book_id'], 'book-a')
            module.save_open_book('book-b')
            module._first_launch = True
            self.assertEqual(module.take_first_launch()['book_id'], 'book-b')
            module.clear_open_book()
            module._first_launch = True
            self.assertIsNone(module.take_first_launch())

    def test_restore_opens_saved_book_without_two_argument_next(self):
        path = ROOT / 'overlay/apps/pocketreader.py'
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'PocketReaderApp')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_restore_sleep_state')
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=[method], decorator_list=[])], type_ignores=[]))
        def strict_next(iterator, *args):
            if args: raise TypeError('next accepts one argument')
            return next(iterator)
        queued = []
        scope = {'REFRESH_ROW':8, 'next':strict_next, 'aio':types.SimpleNamespace(create_task=lambda coro: queued.append(coro) or 'task')}
        exec(compile(code, str(path), 'exec'), scope)
        app = scope['App'](); app.all_books = [{'id': 'book-' + str(i)} for i in range(12)]
        app._show_library_page = lambda: setattr(app, 'books', app.all_books[app.library_page * 8:app.library_page * 8 + 8])
        app._show_selection_led = lambda: None
        app._open_selected = lambda: 'open-book'
        app._restore_sleep_state({'book_id':'book-10', 'library_page':0, 'selected':0})
        self.assertEqual((app.library_page, app.selected, app._download_task), (1, 2, 'task'))
        self.assertEqual(queued, ['open-book'])

    def test_sleep_enables_rtc_pullup_and_saves_open_book(self):
        path = ROOT / 'overlay/apps/pocketreader.py'
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'PocketReaderApp')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_enter_deep_sleep')
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=[method], decorator_list=[])], type_ignores=[]))
        pin = types.SimpleNamespace(value=lambda: 1)
        class Pin:
            IN = 1; PULL_UP = 2
            def __new__(cls, *args): return pin
        memory = {0x600084c4: (1 << 28) | 123}
        calls = []
        machine = types.SimpleNamespace(Pin=Pin, mem32=memory, deepsleep=lambda: calls.append('sleep'))
        esp = types.SimpleNamespace(WAKEUP_ALL_LOW=0, wake_on_ext0=lambda p,l: calls.append(('wake',p,l)))
        sleep = types.SimpleNamespace(save=lambda *args: calls.append(('save',args)))
        scope = {'machine':machine, 'esp32':esp, 'pocketreader_sleep':sleep, 'USER_B_PIN':16,
                 'RTC_GPIO16_PAD':0x600084c4, 'RTC_GPIO_PULLUP':1<<27, 'RTC_GPIO_PULLDOWN':1<<28}
        exec(compile(code, str(path), 'exec'), scope)
        app = scope['App'](); app.reader = types.SimpleNamespace(entry={'id':'book-id'})
        app.detail = True; app.library_page = 2; app.selected = 1; app._bottom_choice = 0
        app._save_reader_progress = lambda: calls.append('progress')
        app._enter_deep_sleep()
        self.assertTrue(memory[0x600084c4] & (1 << 27))
        self.assertFalse(memory[0x600084c4] & (1 << 28))
        self.assertEqual(calls[0:2], ['progress', ('save', ('book-id', 2, 1, 0))])
        self.assertEqual(calls[-1], 'sleep')
        pin.value = lambda: 0
        with self.assertRaises(ValueError): app._enter_deep_sleep()

    def test_idle_timer_waits_for_wifi_and_resets_on_activity(self):
        path = ROOT / 'overlay/apps/pocketreader.py'
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'PocketReaderApp')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_update_device_sleep_timer')
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=[method], decorator_list=[])], type_ignores=[]))
        clock = types.SimpleNamespace(now=300001, ticks_ms=lambda: clock.now, ticks_diff=lambda a,b: a-b)
        button = types.SimpleNamespace(value=False)
        wifi = types.SimpleNamespace(on=True, radio_status=lambda: (wifi.on, wifi.on))
        scope = {'time':clock, 'wifi_setup':wifi, 'UserA':button, 'UserB':button,
                 'PhysicalMenuMinus':button, 'PhysicalMenuPlus':button}
        exec(compile(code, str(path), 'exec'), scope)
        app = scope['App'](); app._sleep_pending = app._sleep_requested = app._setup_pending = False
        app._sleep_timer_minutes = 5; app._device_idle_since = 0
        app._update_device_sleep_timer()
        self.assertFalse(app._sleep_requested)
        wifi.on = False; clock.now += 299999; app._update_device_sleep_timer()
        self.assertFalse(app._sleep_requested)
        clock.now += 1; app._update_device_sleep_timer()
        self.assertTrue(app._sleep_requested)

    def test_both_menu_buttons_trigger_only_after_hold_and_release(self):
        path = ROOT / 'overlay/apps/pocketreader.py'
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'PocketReaderApp')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_handle_menu_sleep_chord')
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App', bases=[], keywords=[], body=[method], decorator_list=[])], type_ignores=[]))
        clock = types.SimpleNamespace(now=0, ticks_ms=lambda: clock.now, ticks_diff=lambda a,b: a-b)
        left = types.SimpleNamespace(value=False, _pressed_state=0)
        right = types.SimpleNamespace(value=False, _pressed_state=0)
        scope = {'time': clock, 'PhysicalMenuMinus': left, 'PhysicalMenuPlus': right,
                 'PRESSED_READ': 7, 'SLEEP_HOLD_MS': 3000}
        exec(compile(code, str(path), 'exec'), scope)
        app = scope['App'](); app._sleep_pending = app._setup_pending = False
        app._settings_menu = app._wifi_menu = True; app.detail = False
        app._menu_sleep_started = None; app._menu_sleep_ready = app._sleep_requested = False
        feedback = []
        app._update_reading_hold_feedback = lambda owner, elapsed: feedback.append((owner, elapsed))
        app._clear_reading_hold_feedback = lambda owner: feedback.append(('clear', owner))
        left.value = right.value = True
        self.assertTrue(app._handle_menu_sleep_chord())
        clock.now = 3000; self.assertTrue(app._handle_menu_sleep_chord())
        self.assertFalse(app._sleep_requested)
        left.value = right.value = False
        self.assertTrue(app._handle_menu_sleep_chord())
        self.assertTrue(app._sleep_requested)
        self.assertEqual(feedback, [('sleep', 0), ('sleep', 3000), ('clear', 'sleep')])
        self.assertEqual((left._pressed_state, right._pressed_state), (7, 7))

    def test_resume_only_on_deep_wake_and_consumed_once(self):
        machine = types.ModuleType('machine')
        machine.DEEPSLEEP_RESET = 4
        machine.reset_cause = lambda: 4
        with patch.dict(sys.modules, {'machine': machine}), tempfile.TemporaryDirectory() as tmp:
            spec = importlib.util.spec_from_file_location('sleep_resume_test', ROOT / 'overlay/lib/pocketreader_sleep.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            module.PATH = str(Path(tmp) / 'resume.json')
            module.save('example-book', 2, 3)
            self.assertEqual(module.peek()['book_id'], 'example-book')
            self.assertEqual(module.take()['library_page'], 2)
            self.assertIsNone(module.take())
            module.save(None, 1, 4)
            machine.reset_cause = lambda: 1
            self.assertIsNone(module.peek())
            self.assertIsNone(module.take())
            self.assertFalse(Path(module.PATH).exists())
