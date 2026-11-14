"""Sleep image validation and display behavior."""
import ast
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_image_helper():
    spec = importlib.util.spec_from_file_location('test_sleep_image_helper', ROOT / 'overlay/lib/sleep_image.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class Connection:
    def __init__(self, data): self.data = data
    def recv(self, size):
        part, self.data = self.data[:size], self.data[size:]
        return part


class SleepImage(unittest.TestCase):
    def test_upload_validates_then_replaces_and_rejects_bad_image(self):
        helper = load_image_helper()
        data = (ROOT / 'assets/sleep/pocketreader-sleep-monochrome-device.png').read_bytes()
        fake = types.ModuleType('lib.large_png')
        fake._read_palette_png = lambda path: (960, 672, b'', iter([b''] * 672), 1)
        package = types.ModuleType('lib'); package.large_png = fake
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'lib': package, 'lib.large_png': fake}):
            helper.PATH = str(Path(tmp) / 'sleep.png')
            Path(helper.PATH).write_bytes(b'old image')
            helper.receive(len(data), data[:100], Connection(data[100:]))
            self.assertEqual(Path(helper.PATH).read_bytes(), data)
            with self.assertRaises(ValueError):
                helper.receive(10, b'not an image', Connection(b''))
            self.assertEqual(Path(helper.PATH).read_bytes(), data)
            with patch.object(helper.os, 'rename', side_effect=OSError('rename failed')):
                with self.assertRaises(OSError):
                    helper.receive(len(data), data, Connection(b''))
            self.assertEqual(Path(helper.PATH).read_bytes(), data)

    def test_fast_launch_dithers_red_and_other_mode_keeps_it(self):
        path = ROOT / 'overlay/lib/pocketreader_display.py'
        tree = ast.parse(path.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ReaderDisplay')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_show_sleep_image')
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='Display', bases=[], keywords=[], body=[method], decorator_list=[])], type_ignores=[]))
        def make_display():
            d = types.SimpleNamespace(_framebuffer_black=bytearray(240), _framebuffer_red=bytearray(240))
            def clear(): d._framebuffer_black[:] = bytes(240); d._framebuffer_red[:] = bytes(240)
            def draw(path): d._framebuffer_red[:] = bytes([255] * 240)
            d.clear = clear; d.draw_png = draw; d.update = lambda: None
            return d
        for quick, existing, partial in ((True, True, False), (True, True, True), (True, False, False), (False, True, False)):
            calls = []
            class Panel:
                def __init__(self): self.buf = bytearray(240)
                async def start(self, **kwargs): calls.append(('start', kwargs))
                async def restart(self): calls.append('restart')
                async def show_still(self): calls.append(('still', bytes(self.buf)))
                async def stop(self): calls.append('stop')
            lib = types.ModuleType('lib')
            partial_module = types.ModuleType('lib.pocketreader_partial')
            partial_module.PartialEPD = Panel
            partial_module.still_table = lambda: 'still LUT'
            display = make_display(); display.update = lambda: calls.append('color update')
            scope = {'display': display, 'sys': types.SimpleNamespace(print_exception=lambda e: None)}
            exec(compile(code, str(path), 'exec'), scope)
            instance = scope['Display'](); instance.panel = Panel() if existing else None
            instance.partial_mode = partial; instance.quick_boot = quick
            async def wait(): pass
            instance._wait_stock = wait
            with patch.dict(sys.modules, {'lib': lib, 'lib.pocketreader_partial': partial_module}):
                asyncio.run(instance._show_sleep_image('sample.png'))
            self.assertIsNone(instance.error)
            if quick:
                self.assertEqual(display._framebuffer_black[0], 0xaa)
                self.assertEqual(display._framebuffer_black[120], 0x55)
                self.assertFalse(any(display._framebuffer_red))
                self.assertNotIn('color update', calls)
                self.assertIn(('still', bytes(display._framebuffer_black)), calls)
                self.assertEqual(calls[-1], 'stop')
                self.assertEqual('restart' in calls, partial)
            else:
                self.assertEqual(display._framebuffer_red[0], 255)
                self.assertIn('color update', calls)
