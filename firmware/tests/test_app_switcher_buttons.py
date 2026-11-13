"""Host-side tests for the app switcher's menu-button contract.

Run with: python3 -m unittest discover -s firmware/tests
"""
import asyncio
import importlib.util
import io
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SWITCHER = Path(__file__).resolve().parents[1] / 'badge/lib/app_switcher.py'


class StopLoop(Exception):
    pass


class Button:
    def __init__(self, pressed=False):
        self.value = pressed
        self.calls = 0
        self._pressed_state = 0

    def pressed(self):
        self.calls += 1
        return self.value


class SwitcherButtonTests(unittest.TestCase):
    def make_switcher(self, app, minus=False, plus=False):
        buttons = [Button(minus), Button(plus), Button(), Button()]
        class FakeDB(dict):
            def flush(self): pass
            def close(self): pass
        apps = types.ModuleType('apps'); apps.__path__ = []
        base = types.ModuleType('apps.base_app'); base.BaseApp = type('BaseApp', (), {})
        lib = types.ModuleType('lib'); lib.__path__ = []
        button_module = types.ModuleType('lib.button')
        button_module.MenuMinus, button_module.MenuPlus, button_module.UserA, button_module.UserB = buttons
        button_module.PRESSED_READ = 2
        led_module = types.ModuleType('lib.led_bar'); led_module.leds = object()
        data = types.ModuleType('data'); data.__path__ = []
        user_apps = types.ModuleType('data.user_apps'); data.user_apps = user_apps
        btree = types.ModuleType('btree'); btree.open = lambda _: FakeDB()
        modules = {'apps': apps, 'apps.base_app': base, 'lib': lib, 'lib.button': button_module,
                   'lib.led_bar': led_module, 'data': data, 'data.user_apps': user_apps, 'btree': btree}
        spec = importlib.util.spec_from_file_location('test_badge_app_switcher', SWITCHER)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules), patch.object(os, 'ilistdir', return_value=[], create=True), \
             patch('builtins.open', side_effect=lambda *_args, **_kwargs: io.BytesIO()):
            spec.loader.exec_module(module)
        async def stop_after_cycle(_):
            raise StopLoop()
        module.aio = types.SimpleNamespace(sleep_ms=stop_after_cycle)
        instance = module.AppSwitcher.__new__(module.AppSwitcher)
        instance.apps = [app]; instance.active_idx = 0; instance.selection_idx = 0; instance.selecting_mode = False
        return instance, buttons

    def run_one_cycle(self, switcher):
        with self.assertRaises(StopLoop):
            asyncio.run(switcher.arun())

    def test_ordinary_apps_keep_existing_menu_behavior(self):
        class App:
            launched = True
            calls = 0
            async def arun(self): self.calls += 1
        app = App(); switcher, buttons = self.make_switcher(app, minus=True)
        self.run_one_cycle(switcher)
        self.assertTrue(switcher.selecting_mode)
        self.assertEqual(app.calls, 1)
        self.assertEqual([buttons[0]._pressed_state, buttons[1]._pressed_state], [2, 2])

    def test_opted_in_app_receives_menu_buttons(self):
        class App:
            captures_menu_buttons = True
            launched = True
            calls = 0
            async def arun(self): self.calls += 1
        app = App(); switcher, buttons = self.make_switcher(app, minus=True, plus=True)
        self.run_one_cycle(switcher)
        self.assertFalse(switcher.selecting_mode)
        self.assertEqual(app.calls, 1)
        self.assertEqual([buttons[0].calls, buttons[1].calls], [0, 0])

    def test_exit_request_returns_to_chooser_and_consumes_buttons(self):
        class App:
            captures_menu_buttons = True
            exit_requested = False
            launched = True
            async def arun(self): self.exit_requested = True
        app = App(); switcher, buttons = self.make_switcher(app)
        self.run_one_cycle(switcher)
        self.assertTrue(switcher.selecting_mode)
        self.assertFalse(app.exit_requested)
        self.assertEqual([button._pressed_state for button in buttons], [2, 2, 2, 2])


if __name__ == '__main__':
    unittest.main()
