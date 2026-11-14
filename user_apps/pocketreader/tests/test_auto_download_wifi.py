"""Starter downloads use temporary Wi-Fi only when necessary."""
import ast
import asyncio
from pathlib import Path
import types
import unittest

PATH = Path(__file__).resolve().parents[1] / 'overlay/apps/pocketreader.py'
module = ast.parse(PATH.read_text())
source = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == 'PocketReaderApp')
methods = [n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name in ('_download_starter_book', '_show_download_status', '_connect_starter_wifi')]

class AutoDownloadWifiTest(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, radio, connected, preferred='Last Wi-Fi', failure=None):
        events = []
        class LibraryError(Exception): pass
        async def connect(ssid, duration, callback, remember=True):
            events.append(('connect', ssid, duration, remember))
            if failure == 'connect': raise OSError('connection failed')
        async def download(entry):
            events.append(('download', entry['id']))
            if failure == 'download': raise OSError('download failed')
        wifi = types.SimpleNamespace(radio_status=lambda:(radio,connected), preferred_ssid=lambda:preferred,
            saved_networks=lambda:[{'ssid':'Other Wi-Fi'}, {'ssid':'Last Wi-Fi'}], connect_saved=connect,
            turn_off=lambda remember=True:events.append(('off',remember)),
            extend_automatic_connection=lambda duration:events.append(('extend',duration)))
        starter = types.SimpleNamespace(download=download)
        code = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[]))
        aio=types.SimpleNamespace(create_task=asyncio.create_task,sleep_ms=lambda _:asyncio.sleep(0))
        scope={'wifi_setup':wifi,'starter_library':starter,'book_library':types.SimpleNamespace(LibraryError=LibraryError),'aio':aio}
        exec(compile(code,str(PATH),'exec'),scope)
        app=scope['App'](); app._wifi_task=None; app._dirty=False
        app.reader_display=types.SimpleNamespace(ready=lambda:True)
        app._draw=lambda:events.append(('display',app.update_status))
        app._update_wifi_indicator=lambda:None
        async def animate(): events.append(('animation',))
        app._animate_download=animate
        try:
            await app._download_starter_book({'id':'book','title':'Book'})
        except Exception as exc:
            events.append(('error',str(exc)))
        return events

    async def test_last_network_is_temporary_when_wifi_was_off(self):
        self.assertEqual(await self.run_case(False,False), [('display','CONNECTING Last Wi-Fi'),('connect','Last Wi-Fi',None,False),('display','DOWNLOADING Book'),('animation',),('download','book'),('extend',120000)])

    async def test_existing_wifi_is_preserved(self):
        self.assertEqual(await self.run_case(True,True), [('display','DOWNLOADING Book'),('animation',),('download','book'),('extend',120000)])

    async def test_failed_download_turns_off_temporary_wifi(self):
        events=await self.run_case(False,False,failure='download')
        self.assertIn(('off',False),events)
        self.assertIn(('error','download failed'),events)

    async def test_failed_connection_turns_off_temporary_wifi(self):
        events=await self.run_case(False,False,failure='connect')
        self.assertIn(('off',False),events)

    async def test_no_preferred_network_prompts_for_selection(self):
        self.assertEqual(await self.run_case(False,False,preferred=''), [('error','Select a Wi-Fi network in settings first')])

if __name__ == '__main__': unittest.main()
