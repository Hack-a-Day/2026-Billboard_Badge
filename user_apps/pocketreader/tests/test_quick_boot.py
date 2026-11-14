"""Check the first display refresh selected for each startup mode."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class QuickBoot(unittest.TestCase):
    def run_startup(self, quick):
        calls = []
        display = types.SimpleNamespace(_framebuffer_black=bytearray(b'book'))
        display.clear = lambda: (calls.append('color clear'), display._framebuffer_black.__setitem__(slice(None), b'    '))
        display.update = lambda: calls.append('color update')
        display.is_done_updating = lambda: True

        class Panel:
            def __init__(self):
                self.buf = bytearray(4)
            async def start(self, **kwargs):
                calls.append(('start', kwargs))
            async def show_still(self):
                calls.append(('draw', bytes(self.buf)))
            async def show(self):
                calls.append('partial')

        lib = types.ModuleType('lib')
        epd = types.ModuleType('lib.epd'); epd.display = display
        partial = types.ModuleType('lib.pocketreader_partial'); partial.PartialEPD = Panel
        path = Path(__file__).resolve().parents[1] / 'overlay/lib/pocketreader_display.py'
        spec = importlib.util.spec_from_file_location('quick_boot_display_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'lib': lib, 'lib.epd': epd, 'lib.pocketreader_partial': partial}):
            spec.loader.exec_module(module)
            reader = module.ReaderDisplay()
            reader.quick_boot = quick
            asyncio.run(reader._show_frame())
            asyncio.run(reader._show_frame())
            reader.partial_mode = True
            asyncio.run(reader._show_frame(partial=True))
        return calls

    def test_normal_start_keeps_color_clear(self):
        calls = self.run_startup(False)
        self.assertEqual(calls.count('color update'), 1)
        self.assertIn(('start', {'clean_red': False, 'known_white': True}), calls)
        self.assertEqual(calls.count(('draw', b'book')), 2)
        self.assertEqual(calls.count('partial'), 1)

    def test_quick_start_uses_black_and_white_wipe(self):
        calls = self.run_startup(True)
        self.assertNotIn('color update', calls)
        self.assertNotIn('color clear', calls)
        self.assertIn(('start', {'clean_red': False, 'known_white': False}), calls)
        self.assertEqual(calls.count(('draw', b'book')), 2)
        self.assertEqual(calls.count('partial'), 1)
