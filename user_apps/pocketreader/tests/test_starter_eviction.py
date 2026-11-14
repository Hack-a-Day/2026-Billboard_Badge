"""Only recoverable starter books can be evicted for space."""
import ast
import binascii
import hashlib
import os
from pathlib import Path
import tempfile
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'overlay/lib/book_library.py'
node = next(n for n in ast.parse(SOURCE.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'ensure_space')

class EvictionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.state = {}; self.read = {}; self.removed = []
        class Error(Exception): pass
        self.Error = Error
        self.package = lambda entry: str(self.root / (entry['id'] + '.prb'))
        root = self.root
        class FakeOS:
            stat = staticmethod(os.stat)
            remove = staticmethod(os.remove)
            @staticmethod
            def statvfs(_):
                freed = sum(size for path, size in initial_files.items() if not path.exists())
                return (1, 1, 0, freed)
        initial_files = self.initial_files = {}
        def remove_download(entry):
            self.removed.append(entry['id']); os.remove(self.package(entry)); self.state.pop(entry['id'])
        scope = {'installed_state':lambda:self.state, '_read_json':lambda path, default:{'books':self.read},
                 'READING_STATE_PATH':'reading', '_exists':os.path.exists, 'package_path':self.package,
                 'BOOK_DIR':str(root), 'os':FakeOS, 'hashlib':hashlib, 'binascii':binascii, 'LibraryError':Error, 'remove_download':remove_download}
        code=ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[]));exec(compile(code,str(SOURCE),'exec'),scope)
        self.ensure=scope['ensure_space']

    def tearDown(self): self.tmp.cleanup()

    def add(self, ident, digest, order, read=None):
        for suffix, size in (('.prb',100),('.pri',50)):
            path=self.root/(ident+suffix); path.write_bytes((ident.encode()*size)[:size]); self.initial_files[path]=size
        (self.root/(ident+'.prp')).write_text('bookmarks and progress')
        self.state[ident]={'local_only':True,'sha256':hashlib.sha256((self.root/(ident+'.prb')).read_bytes()).hexdigest(),'download_order':order}
        if read is not None: self.read[ident]=read

    def test_oldest_unread_catalog_copy_evicted_first_and_progress_kept(self):
        self.add('old','a',1);self.add('new','b',2);self.add('read','c',0,read=1);self.add('personal','d',0)
        self.ensure(120,'target',recoverable={**{key:self.state[key]['sha256'] for key in ('old','new','read')},'personal':'different'})
        self.assertEqual(self.removed,['old'])
        self.assertFalse((self.root/'old.pri').exists())
        self.assertEqual((self.root/'old.prp').read_text(),'bookmarks and progress')
        self.assertTrue((self.root/'personal.prb').exists())

    def test_modified_local_file_is_not_assumed_recoverable(self):
        self.add('changed','a',1)
        expected=self.state['changed']['sha256']
        (self.root/'changed.prb').write_bytes(b'different data')
        with self.assertRaises(self.Error): self.ensure(10,'target',recoverable={'changed':expected})
        self.assertEqual(self.removed,[])

    def test_insufficient_recoverable_space_deletes_nothing(self):
        self.add('old','a',1);self.add('personal','b',2)
        with self.assertRaises(self.Error): self.ensure(200,'target',recoverable={'old':self.state['old']['sha256']})
        self.assertEqual(self.removed,[])
        self.assertTrue((self.root/'old.prb').exists())

    def test_qualified_read_books_follow_unread_in_lru_order(self):
        self.add('first','a',3,read=3); self.add('second','b',2,read=9); self.add('unread','c',10)
        self.ensure(250,'target',recoverable={key:self.state[key]['sha256'] for key in ('first','second','unread')})
        self.assertEqual(self.removed,['unread','first'])

if __name__ == '__main__': unittest.main()
