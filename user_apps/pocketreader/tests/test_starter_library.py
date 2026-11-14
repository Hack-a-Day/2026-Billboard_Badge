"""Public starter library stays opt-in and verifies downloads."""
import asyncio, hashlib, importlib.util, io, json, sys, tempfile, types, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]

class StarterLibraryTest(unittest.TestCase):
    def setup_modules(self, tmp):
        root=Path(tmp); files={}; registered=[]
        class Error(Exception): pass
        def read_json(path, fallback):
            try: return json.loads(Path(path).read_text())
            except (OSError, ValueError): return fallback
        def write_json(path, value): Path(path).write_text(json.dumps(value))
        lib=types.ModuleType('lib'); reader=types.ModuleType('lib.book_library')
        reader.PREFERENCES_PATH=str(root/'prefs.json'); reader.BOOK_DIR=str(root/'books')
        reader.LibraryError=Error; reader._read_json=read_json; reader._write_json=write_json
        reader._safe_id=lambda ident: ident if ident and '/' not in ident and '..' not in ident else (_ for _ in ()).throw(Error('bad id'))
        reader.package_path=lambda entry: reader.BOOK_DIR+'/'+entry['id']+'.prb'
        reader._exists=lambda path: Path(path).exists()
        reader.ensure_space=lambda required, protected_id, recoverable=None: None
        wifi=types.ModuleType('lib.wifi_setup'); wifi.radio_status=lambda: (True,True)
        transfer=types.ModuleType('lib.book_transfer'); transfer.register_installed_book=lambda ident,sha:registered.append((ident,sha))
        lib.book_library=reader;lib.wifi_setup=wifi;lib.book_transfer=transfer
        spec=importlib.util.spec_from_file_location('starter_under_test',ROOT/'overlay/lib/starter_library.py');module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'lib':lib,'lib.book_library':reader,'lib.wifi_setup':wifi,'lib.book_transfer':transfer}): spec.loader.exec_module(module)
        module.CATALOG_PATH=str(root/'catalog.json')
        async def sleep_ms(_): pass
        class Stream:
            def __init__(self):
                self.headers=iter([b'HTTP/1.0 200 OK\r\n', b'\r\n'])
                self.body=io.BytesIO(files['device.json'])
            async def readline(self): return next(self.headers, b'')
            async def read(self, size):
                await asyncio.sleep(0)
                return self.body.read(size)
            def write(self, data): pass
            async def drain(self): pass
            def close(self): pass
            async def wait_closed(self): pass
        async def open_connection(host, port, ssl=None):
            stream=Stream(); return stream,stream
        module.aio=types.SimpleNamespace(sleep_ms=sleep_ms, wait_for=asyncio.wait_for, open_connection=open_connection)
        class Response:
            status_code=200
            def __init__(self,body): self.content=body;self.raw=io.BytesIO(body)
            def close(self): pass
        module._request=lambda url:Response(files[url.rsplit('/',1)[-1]])
        async def open_download(url, expected_size):
            data=io.BytesIO(files[url.rsplit('/',1)[-1]])
            class Reader:
                async def read(self, size):
                    await asyncio.sleep(0)
                    return data.read(size)
            class Writer:
                def close(self): pass
                async def wait_closed(self): pass
            return Reader(), Writer()
        module._open_download=open_download
        return module,reader,transfer,files,registered,lib

    def test_opt_in_refresh_download_and_preserve_local_copy(self):
        payload=b'{"id":"pg1"}\nSome text\n';sha=hashlib.sha256(payload).hexdigest()
        index=json.dumps({'version':1,'book_id':'pg1','sha256':sha,'sizes':{},'chapters':[]}).encode()
        item={'id':'pg1','title':'Test','author':'Author','size':len(payload),'sha256':sha,'index_size':len(index),'index_sha256':hashlib.sha256(index).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            app,reader,transfer,files,registered,lib=self.setup_modules(temp)
            files.update({'device.json':json.dumps({'schema':1,'books':[item]}).encode(),'pg1.prb':payload,'pg1.pri':index})
            self.assertEqual(app.load_catalog(),[]);self.assertEqual(asyncio.run(app.refresh()),[])
            app.set_enabled(True);self.assertEqual(asyncio.run(app.refresh()),[item])
            with patch.dict(sys.modules,{'lib':lib,'lib.book_transfer':transfer}): path=asyncio.run(app.download(item))
            self.assertEqual(Path(path).read_bytes(),payload);self.assertEqual(Path(reader.BOOK_DIR,'pg1.pri').read_bytes(),index)
            self.assertEqual(registered,[(item['id'],sha)])
            app.set_enabled(False);self.assertEqual(app.load_catalog(),[]);self.assertEqual(Path(path).read_bytes(),payload)

    def test_transfer_yields_so_light_animation_can_advance(self):
        payload=b'x'*8192
        with tempfile.TemporaryDirectory() as temp:
            app,reader,transfer,files,registered,lib=self.setup_modules(temp)
            files['book.prb']=payload
            async def run():
                ticks=[]; finished=False
                async def animation():
                    while not finished:
                        ticks.append(1)
                        await asyncio.sleep(0)
                task=asyncio.create_task(animation())
                try:
                    await app._fetch_file(app.BASE+'book.prb',str(Path(temp)/'book'),len(payload),hashlib.sha256(payload).hexdigest())
                finally:
                    finished=True
                    await task
                return len(ticks)
            self.assertGreater(asyncio.run(run()),1)

    def test_invalid_hash_never_installs_book(self):
        payload=b'right';sha=hashlib.sha256(payload).hexdigest()
        index=json.dumps({'version':1,'book_id':'pg1','sha256':sha,'sizes':{},'chapters':[]}).encode()
        item={'id':'pg1','title':'Test','author':'Author','size':len(payload),'sha256':sha,'index_size':len(index),'index_sha256':hashlib.sha256(index).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            app,reader,transfer,files,registered,lib=self.setup_modules(temp)
            files.update({'pg1.prb':b'wrong','pg1.pri':index,'device.json':json.dumps({'schema':1,'books':[item]}).encode()});app.set_enabled(True)
            with patch.dict(sys.modules,{'lib':lib,'lib.book_transfer':transfer}):
                with self.assertRaises(reader.LibraryError): asyncio.run(app.download(item))
            self.assertFalse(Path(reader.BOOK_DIR,'pg1.prb').exists());self.assertEqual(registered,[])

    def test_download_uses_fresh_catalog_and_preflights_before_eviction(self):
        payload=b'book'; digest=hashlib.sha256(payload).hexdigest()
        index=json.dumps({'version':1,'book_id':'pg1','sha256':digest,'sizes':{},'chapters':[]}).encode()
        item={'id':'pg1','title':'Test','author':'Author','size':len(payload),'sha256':digest,
              'index_size':len(index),'index_sha256':hashlib.sha256(index).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            app,reader,transfer,files,registered,lib=self.setup_modules(temp)
            files.update({'device.json':json.dumps({'schema':1,'books':[item]}).encode(),'pg1.prb':payload,'pg1.pri':index})
            app.set_enabled(True); events=[]; original=app._open_download
            async def checked(url,size):
                events.append(('preflight',url.rsplit('/',1)[-1],size))
                return await original(url,size)
            app._open_download=checked
            reader.ensure_space=lambda *args,**kwargs:events.append(('space',))
            stale=dict(item,index_size=1)
            with patch.dict(sys.modules,{'lib':lib,'lib.book_transfer':transfer}): asyncio.run(app.download(stale))
            self.assertEqual(events[:3],[('preflight','pg1.pri',len(index)),('preflight','pg1.prb',len(payload)),('space',)])
            self.assertEqual(app.load_catalog()[0]['index_size'],len(index))

    def test_preflight_failure_cannot_trigger_eviction(self):
        payload=b'book'; digest=hashlib.sha256(payload).hexdigest()
        index=json.dumps({'version':1,'book_id':'pg1','sha256':digest,'sizes':{},'chapters':[]}).encode()
        item={'id':'pg1','title':'Test','author':'Author','size':len(payload),'sha256':digest,
              'index_size':len(index),'index_sha256':hashlib.sha256(index).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            app,reader,transfer,files,registered,lib=self.setup_modules(temp)
            files['device.json']=json.dumps({'schema':1,'books':[item]}).encode();app.set_enabled(True)
            evictions=[];reader.ensure_space=lambda *args,**kwargs:evictions.append(True)
            async def unavailable(*args): raise reader.LibraryError('Download not available')
            app._open_download=unavailable
            with patch.dict(sys.modules,{'lib':lib,'lib.book_transfer':transfer}):
                with self.assertRaises(reader.LibraryError): asyncio.run(app.download(item))
            self.assertEqual(evictions,[])

    def test_cancellation_removes_partial_files(self):
        payload=b'book'; digest=hashlib.sha256(payload).hexdigest()
        index=json.dumps({'version':1,'book_id':'pg1','sha256':digest,'sizes':{},'chapters':[]}).encode()
        item={'id':'pg1','title':'Test','author':'Author','size':len(payload),'sha256':digest,
              'index_size':len(index),'index_sha256':hashlib.sha256(index).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            app,reader,transfer,files,registered,lib=self.setup_modules(temp)
            files.update({'device.json':json.dumps({'schema':1,'books':[item]}).encode(),'pg1.prb':payload,'pg1.pri':index})
            app.set_enabled(True)
            async def run():
                started=asyncio.Event()
                async def partial(name,temporary,size,sha):
                    Path(temporary).write_bytes(b'partial')
                    started.set()
                    await asyncio.Event().wait()
                app._fetch_starter_file=partial
                with patch.dict(sys.modules,{'lib':lib,'lib.book_transfer':transfer}):
                    task=asyncio.create_task(app.download(item))
                    await started.wait()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await task
            asyncio.run(run())
            self.assertEqual(list(Path(reader.BOOK_DIR).glob('*.tmp')),[])
            self.assertEqual(registered,[])

    def test_catalog_reports_unavailable_without_changing_cached_books(self):
        with tempfile.TemporaryDirectory() as temp:
            app,reader,transfer,files,registered,lib=self.setup_modules(temp)
            app.set_enabled(True)
            Path(app.CATALOG_PATH).write_text('{"schema":1,"books":[]}')
            async def unavailable(): raise OSError('not connected')
            app._catalog_bytes=unavailable
            with self.assertRaises(reader.LibraryError): asyncio.run(app.refresh())
            self.assertEqual(app.load_catalog(),[])

if __name__=='__main__': unittest.main()
