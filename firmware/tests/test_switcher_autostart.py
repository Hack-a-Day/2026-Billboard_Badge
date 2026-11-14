"""Test startup and exit behavior with simulated badge buttons and LEDs."""
import ast
import asyncio
import io
from pathlib import Path
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'badge/lib/app_switcher.py'


class StopLoop(Exception):
    pass


class SwitcherAutostart(unittest.TestCase):
    def setup_switcher(self, count, exit_after_run=False):
        tree = ast.parse(SOURCE.read_text())
        switcher = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'AppSwitcher')
        class BaseApp:
            pass
        runs, menus = [], []
        class Reader(BaseApp):
            name = 'Reader'
            captures_menu_buttons = True
            exit_requested = False
            launched = False
            async def arun(self):
                runs.append(self.launched)
                MenuButton.next = exit_after_run
        class Other(BaseApp):
            name = 'Other'
            captures_menu_buttons = True
            exit_requested = False
            launched = False
            async def arun(self):
                runs.append(self.launched)
        appmodule = types.ModuleType('apps.reader')
        if count: appmodule.Reader = Reader
        if count > 1: appmodule.Other = Other
        user_apps = types.ModuleType('data.user_apps')
        user_apps.reader = appmodule
        data = types.ModuleType('data'); data.user_apps = user_apps
        class Button:
            _pressed_state = 0
            @staticmethod
            def pressed(): return False
        class MenuButton(Button):
            next = False
            @classmethod
            def pressed(cls):
                value = cls.next
                cls.next = False
                return value
        class FakeLEDs:
            def menu_display(self, index, length): menus.append((index, length))
            def clear_sequence(self): pass
            def fill(self, color): pass
            def write(self): pass
        class FakeDB(dict):
            def flush(self): pass
            def close(self): pass
        class FakeAio:
            ticks = 0
            @classmethod
            async def sleep_ms(cls, ms):
                cls.ticks += 1
                if cls.ticks >= (3 if exit_after_run else 2): raise StopLoop()
        scope = {'aio': FakeAio, 'os': types.SimpleNamespace(ilistdir=lambda _: [('reader.py', 0, 0, 0)]),
                 'sys': types.SimpleNamespace(print_exception=lambda exc: None),
                 'btree': types.SimpleNamespace(open=lambda _: FakeDB()), 'BaseApp': BaseApp,
                 'MenuMinus': MenuButton, 'MenuPlus': Button, 'UserA': Button, 'UserB': Button,
                 'PRESSED_READ': 1, 'leds': FakeLEDs(), 'APPMOD_DENYLIST': {'base_app', 'template'}}
        code = ast.fix_missing_locations(ast.Module(body=[switcher], type_ignores=[]))
        exec(compile(code, str(SOURCE), 'exec'), scope)
        with patch.dict('sys.modules', {'data': data, 'data.user_apps': user_apps}), \
             patch('builtins.open', side_effect=lambda path, mode: io.StringIO() if mode == 'w' else io.BytesIO()):
            instance = scope['AppSwitcher']()
        return instance, runs, menus

    def test_only_app_launches_once_and_exit_returns_to_chooser(self):
        instance, runs, menus = self.setup_switcher(1, exit_after_run=True)
        with self.assertRaises(StopLoop): asyncio.run(instance.arun())
        self.assertEqual(runs, [True, False])
        self.assertTrue(instance.selecting_mode)
        self.assertEqual(menus, [(0, 1)])

    def test_multiple_apps_show_chooser(self):
        instance, runs, menus = self.setup_switcher(2)
        with self.assertRaises(StopLoop): asyncio.run(instance.arun())
        self.assertEqual(runs, [])
        self.assertEqual(menus, [(0, 2), (0, 2)])

    def test_no_apps_does_not_attempt_to_select(self):
        instance, runs, menus = self.setup_switcher(0)
        with self.assertRaises(StopLoop): asyncio.run(instance.arun())
        self.assertEqual(runs, [])
        self.assertEqual(menus, [])
