"""Checks for the local-only reader and its starter books."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalRelease(unittest.TestCase):
    def test_installer_preserves_custom_sleep_image(self):
        installer = load('release_installer_image', ROOT / 'install.py')
        with patch.object(installer.subprocess, 'run') as execute, patch.object(installer, 'run') as copy:
            execute.return_value = types.SimpleNamespace(stdout='True\n')
            installer.install_default_image(None, False)
            copy.assert_not_called()

    def test_installer_replaces_only_previous_default_image(self):
        installer = load('release_installer_image_upgrade', ROOT / 'install.py')
        with patch.object(installer.subprocess, 'run') as execute, patch.object(installer, 'run') as copy:
            execute.side_effect = [types.SimpleNamespace(stdout='True\n'),
                                   types.SimpleNamespace(stdout=installer.OLD_SLEEP_IMAGE_SHA256 + '\n')]
            installer.install_default_image(None, False)
            copy.assert_called_once()
            self.assertIn('floyd-steinberg', copy.call_args.args[0][-2])

    def test_existing_sample_is_registered_without_overwriting(self):
        installer = load('release_installer', ROOT / 'install.py')
        with patch.object(installer.subprocess, 'run') as execute, patch.object(installer, 'run') as copy:
            execute.return_value = types.SimpleNamespace(stdout='True True\n')
            installer.install_sample(None, 'the-time-machine-43d66b728e', False)
            self.assertEqual(copy.call_count, 1)
            self.assertIn("sys.modules.pop('lib.book_transfer'", copy.call_args.args[0][-1])

    def test_sample_with_only_index_is_safely_restored(self):
        installer = load('release_installer', ROOT / 'install.py')
        with patch.object(installer.subprocess, 'run') as execute, patch.object(installer, 'run') as copy:
            execute.return_value = types.SimpleNamespace(stdout='False True\n')
            installer.install_sample(None, 'the-time-machine-43d66b728e', False)
            calls = [call.args[0] for call in copy.call_args_list]
            self.assertEqual(len(calls), 4)
            self.assertTrue(calls[0][-1].endswith('.pri.installing'))
            self.assertIn('os.rename(staged, index)', calls[1][-1])
            self.assertTrue(calls[2][-1].endswith('.prb'))
            self.assertIn('register_installed_book', calls[3][-1])
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                old = root / 'book.pri'; staged = root / 'book.pri.installing'; previous = root / 'book.pri.replaced'
                old.write_text('old'); staged.write_text('new')
                class FakeOS(types.ModuleType):
                    def stat(self, path): return self.resolve(path).stat()
                    def remove(self, path): self.resolve(path).unlink()
                    def rename(self, src, dst):
                        if src.endswith('.installing'): raise OSError('rename failed')
                        self.resolve(src).rename(self.resolve(dst))
                    def resolve(self, path):
                        if path.endswith('.pri.installing'): return staged
                        if path.endswith('.pri.replaced'): return previous
                        return old
                with patch.dict(sys.modules, {'os': FakeOS('os')}), self.assertRaises(OSError):
                    exec(calls[1][-1], {})
                self.assertEqual(old.read_text(), 'old')

    def test_starter_books_register_and_indexes_match(self):
        lib_package = types.ModuleType('lib')
        with patch.dict(sys.modules, {'network': types.ModuleType('network'), 'lib': lib_package}):
            for name in ('pocketreader_font_data', 'pocketreader_pagination'):
                setattr(lib_package, name, load(name, ROOT / 'overlay/lib' / (name + '.py')))
            lib_package.wifi_setup = types.ModuleType('wifi_setup')
            library = load('release_book_library', ROOT / 'overlay/lib/book_library.py')
            lib_package.book_library = library
            transfer = load('release_book_transfer', ROOT / 'overlay/lib/book_transfer.py')
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                library.BOOK_DIR = str(work)
                library.LOCAL_CATALOG_PATH = str(work / 'catalog.json')
                library.CACHE_STATE_PATH = str(work / 'state.json')
                library.READING_STATE_PATH = str(work / 'reading.json')
                for book in sorted((ROOT / 'books').glob('*.prb')):
                    shutil.copy(book, work / book.name)
                    transfer.register_installed_book(book.stem)
                    before = Path(library.CACHE_STATE_PATH).read_bytes()
                    transfer.register_installed_book(book.stem, hashlib.sha256(book.read_bytes()).hexdigest())
                    self.assertEqual(before, Path(library.CACHE_STATE_PATH).read_bytes())
                    with self.assertRaises(ValueError):
                        transfer.register_installed_book(book.stem, 'wrong-digest')
                    sidecar = json.loads(book.with_suffix('.pri').read_text())
                    self.assertEqual(sidecar['sha256'], hashlib.sha256(book.read_bytes()).hexdigest())
                    self.assertTrue(sidecar['chapters'])
                    body = book.read_bytes().split(b'\n', 1)[1]
                    self.assertIn(b'\n\x1e\n', body)
                    self.assertNotIn(b'PROJECT GUTENBERG', body.upper())
                    self.assertEqual(len(sidecar['sizes']), 12)
                    for size, entry in sidecar['sizes'].items():
                        pages = lib_package.pocketreader_pagination.unpack_page_positions(entry['pages'])
                        starts = {page[2] for page in pages}
                        self.assertTrue(all(chapter[0] in starts for chapter in sidecar['chapters']),
                                        (book.name, size))
                entries = library.local_books()
                self.assertEqual(len(entries), 2)
                self.assertTrue(all(library.has_download(entry) for entry in entries))

