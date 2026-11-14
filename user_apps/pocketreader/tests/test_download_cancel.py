"""Cancellation returns to the library instead of surfacing a book error."""
import ast
import asyncio
from pathlib import Path
import types
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'overlay/apps/pocketreader.py'
cls=next(n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='PocketReaderApp')
method=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_open_selected')

class DownloadCancel(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_returns_to_library_and_releases_operation(self):
        events=[];started=asyncio.Event()
        wifi=types.SimpleNamespace(begin_operation=lambda:events.append('begin'),end_operation=lambda:events.append('end'))
        library=types.SimpleNamespace(load_progress=lambda:{},has_download=lambda entry:False)
        scope={'wifi_setup':wifi,'book_library':library,'aio':asyncio}
        code=ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='App',bases=[],keywords=[],body=[method],decorator_list=[])],type_ignores=[]))
        exec(compile(code,str(SOURCE),'exec'),scope);app=scope['App']()
        app.books=[{'id':'one','title':'One','package':'one.prb','_starter':True}];app.selected=0
        app._load_cached_catalog=lambda:events.append('reload')
        app._close_reader=lambda:events.append('close')
        app._show_selection_led=lambda:events.append('selection')
        async def download(entry):
            try:
                started.set()
                await asyncio.Event().wait()
            finally: events.append('cleanup')
        app._download_starter_book=download
        task=asyncio.create_task(app._open_selected());app._download_task=task
        await started.wait();self.assertTrue(app._download_cancellable)
        task.cancel();await task
        self.assertEqual(app.update_status,'DOWNLOAD CANCELED')
        self.assertFalse(app._download_cancellable);self.assertIsNone(app._download_task)
        self.assertEqual(events,['begin','cleanup','reload','close','selection','end'])

if __name__=='__main__':unittest.main()
