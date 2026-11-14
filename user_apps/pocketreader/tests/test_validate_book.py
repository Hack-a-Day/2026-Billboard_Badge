"""Validate the documented interchange format and reject mismatched sidecars."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('validator',ROOT/'validate_book.py')
validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)

class ValidateBook(unittest.TestCase):
    def test_bundled_books_and_indexes(self):
        for book in (ROOT/'books').glob('*.prb'):
            with self.subTest(book=book.name):
                result=validator.validate_book(book,book.with_suffix('.pri'))
                self.assertEqual(result['size_indexes'],12)
                self.assertGreater(result['chapter_count'],0)

    def test_markup_book_and_mismatched_index(self):
        with tempfile.TemporaryDirectory() as directory:
            book=Path(directory)/'book.prb';index=book.with_suffix('.pri')
            header={'schema':4,'id':'book','title':'Example','word_count':3}
            book.write_bytes(json.dumps(header).encode()+b'\n\x1fHExample\n\x1e\nTwo words\n')
            index.write_text(json.dumps({'version':1,'book_id':'book','sha256':hashlib.sha256(book.read_bytes()).hexdigest(),
                'chapters':[[0,'Start']],'sizes':{'medium':{'layout_version':2,'heading_metrics_sha256':'a'*64,
                'encoding':'delta-varint-base64','pages':'AAAA'}}}))
            self.assertEqual(validator.validate_book(book,index)['size_indexes'],1)
            book.write_bytes(book.read_bytes()+b' changed')
            with self.assertRaises(ValueError):validator.validate_book(book,index)

    def test_unknown_markup_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            book=Path(directory)/'book.prb';book.write_bytes(b'{"schema":4,"id":"book","word_count":1}\n\x1fXword\n')
            with self.assertRaises(ValueError):validator.validate_book(book)

if __name__=='__main__':unittest.main()
