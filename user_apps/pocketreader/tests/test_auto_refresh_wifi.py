"""Refreshing the starter library temporarily connects to the last network."""
import ast
import asyncio
from pathlib import Path
import types
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'overlay/apps/pocketreader.py'
cls=next(n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='PocketReaderApp')
names={'_show_download_status','_connect_starter_wifi','_refresh_catalog'}
methods=[n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name in names]

class RefreshWifi(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, connected, failure=None):
        events=[]
        class Error(Exception): pass
        wifi=types.SimpleNamespace(radio_status=lambda:(connected,connected), preferred_ssid=lambda:'last network',
             saved_networks=lambda:[],begin_operation=lambda:events.append('begin'),end_operation=lambda:events.append('end'),
             turn_off=lambda remember=True:events.append(('off',remember)))
        async def connect(ssid,duration,callback,remember=True):
            events.append(('connect',ssid,remember))
            if failure=='connect':raise Error('connection failed')
        wifi.connect_saved=connect
        async def refresh():
            events.append('refresh')
            if failure=='refresh':raise Error('catalog failed')
        starter=types.SimpleNamespace(enabled=lambda:True,refresh=refresh)
        aio=types.SimpleNamespace(sleep_ms=lambda _:asyncio.sleep(0))
        scope={'wifi_setup':wifi,'starter_library':starter,'book_library':types.SimpleNamespace(LibraryError=Error),'aio':aio}
        code=ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[]))
        exec(compile(code,str(SOURCE),'exec'),scope);app=scope['App']()
        app._wifi_task=None;app._catalog_task=object();app.reader_display=types.SimpleNamespace(ready=lambda:True)
        app._draw=lambda:events.append(('display',app.update_status))
        app._update_wifi_indicator=lambda:None;app._load_cached_catalog=lambda:events.append('load')
        app.detail=app._settings_menu=app._wifi_menu=False
        app._select_first_on_library_page=lambda:None;app._show_selection_led=lambda:None
        await app._refresh_catalog()
        return events,app

    async def test_offline_refresh_connects_and_turns_off(self):
        events,app=await self.run_case(False)
        self.assertEqual(events,['begin',('display','CONNECTING last network'),('connect','last network',False),'refresh','load',('off',False),'end'])
        self.assertEqual(app.update_status,'LIBRARY UPDATED')

    async def test_already_connected_remains_on(self):
        events,app=await self.run_case(True)
        self.assertNotIn(('off',False),events)
        self.assertEqual(app.update_status,'LIBRARY UPDATED')

    async def test_failed_connection_turns_off_and_preserves_catalog(self):
        events,app=await self.run_case(False,'connect')
        self.assertEqual(events.count(('off',False)),1)
        self.assertNotIn('refresh',events)
        self.assertIn('load',events)
        self.assertIn('connection failed',app.update_status)

if __name__=='__main__':unittest.main()
