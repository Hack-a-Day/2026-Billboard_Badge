"""Bounded transfers of badge books and their sidecars."""
import binascii
import hashlib
import json
import os
from lib import book_library as lib

MAX_UPLOAD = 4 * 1024 * 1024
MAX_SIDECAR = 1024 * 1024
_generation = 0
_open_book_id = None


def generation():
    return _generation


def set_open_book(book_id):
    global _open_book_id
    _open_book_id = book_id


def _is_transfer_file(name):
    # MicroPython 1.24 accepts one string suffix, not a tuple of suffixes.
    return name.endswith('.prb') or name.endswith('.prp') or name.endswith('.pri')


def _safe_name(name):
    name = str(name)
    lower = name.lower()
    if '/' in name or '\\' in name or not _is_transfer_file(lower):
        raise ValueError('Use a PRB, PRP, or PRI file')
    stem, ext = name.rsplit('.', 1)
    lib._safe_id(stem)
    return stem, ext.lower()


def list_files():
    try:
        names = os.listdir(lib.BOOK_DIR)
    except OSError:
        return []
    names = [name for name in names if _is_transfer_file(name)]
    # Expose legacy preferences under the new transfer name without deleting them.
    for name in os.listdir(lib.BOOK_DIR):
        if name.endswith('.prxl') and name[:-5] + '.prp' not in names:
            names.append(name[:-5] + '.prp')
    return sorted(names)


def download_path(name):
    stem, ext = _safe_name(name)
    if stem == _open_book_id:
        raise ValueError('Close this book on the badge before downloading its files')
    path = lib.BOOK_DIR + '/' + stem + '.' + ext
    if ext == 'prp' and not lib._exists(path):
        path = lib.BOOK_DIR + '/' + stem + '.prxl'
    if not lib._exists(path):
        raise ValueError('File not found')
    return path


def file_size(name):
    """Report size even when a book is open; preferences may use the legacy name."""
    stem, ext = _safe_name(name)
    path = lib.BOOK_DIR + '/' + stem + '.' + ext
    if ext == 'prp' and not lib._exists(path):
        path = lib.BOOK_DIR + '/' + stem + '.prxl'
    return os.stat(path)[6]


def storage_space():
    stats = os.statvfs(lib.BOOK_DIR)
    block = stats[1] or stats[0]
    total, free = block * stats[2], block * stats[3]
    return {'total': total, 'used': total - free, 'free': free}


def register_installed_book(book_id, expected_sha256=None):
    """Register a book already copied to the badge without changing its files."""
    book_id = lib._safe_id(book_id)
    path = lib.BOOK_DIR + '/' + book_id + '.prb'
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        header = source.readline(65537)
        if len(header) > 65536:
            raise ValueError('Book header is too large')
        info = json.loads(header)
        if not isinstance(info, dict) or info.get('id') != book_id or info.get('schema') not in (1, 2, 3):
            raise ValueError('Invalid book file')
        digest.update(header)
        while True:
            chunk = source.read(4096)
            if not chunk:
                break
            digest.update(chunk)
    length = os.stat(path)[6]
    digest_hex = binascii.hexlify(digest.digest()).decode()
    if expected_sha256 and digest_hex != expected_sha256:
        raise ValueError('Existing book differs from the sample; leaving it untouched')
    catalog = lib._read_json(lib.LOCAL_CATALOG_PATH, {})
    state = lib.installed_state()
    if (state.get(book_id, {}).get('sha256') == digest_hex
            and any(entry.get('id') == book_id for entry in catalog.get('books', []))):
        return
    books = [entry for entry in catalog.get('books', []) if entry.get('id') != book_id]
    books.append({'id': book_id, 'title': info['title'], 'author': info['author'],
                  'year': info.get('year', 0), 'word_count': info.get('word_count', 0),
                  'series': info.get('series', ''), 'series_position': info.get('series_position', ''),
                  'size': length, 'sha256': digest_hex, 'package': book_id + '.prb', 'local_only': True})
    state[book_id] = {'sha256': digest_hex, 'size': length, 'local_only': True,
                      'download_order': lib._next_sequence()}
    lib._write_json(lib.LOCAL_CATALOG_PATH, {'books': books})
    lib._write_json(lib.CACHE_STATE_PATH, state)


def list_books():
    """List installed books and available sidecars without reading book bodies."""
    try:
        files = os.listdir(lib.BOOK_DIR)
    except OSError:
        return []
    result = []
    installed = lib.installed_state()
    for filename in files:
        if not filename.endswith('.prb'):
            continue
        stem = filename[:-4]
        try:
            lib._safe_id(stem)
            with open(lib.BOOK_DIR + '/' + filename, 'rb') as source:
                header = source.readline(65537)
            if len(header) > 65536:
                continue
            metadata = json.loads(header)
            if not isinstance(metadata, dict) or metadata.get('id') != stem:
                continue
            result.append({'id': stem, 'title': str(metadata.get('title', stem)),
                           'author': str(metadata.get('author', 'Unknown Author')),
                           'year': metadata.get('year', 0),
                           'sha256': installed.get(stem, {}).get('sha256', ''),
                           'open': stem == _open_book_id,
                           'prp': stem + '.prp' in files or stem + '.prxl' in files,
                           'pri': stem + '.pri' in files})
        except (OSError, ValueError, TypeError):
            continue
    return result


def remove_book(book_id):
    """Remove the device copy while retaining its reading state."""
    global _generation
    book_id = lib._safe_id(book_id)
    if book_id == _open_book_id:
        raise ValueError('Close this book on the badge before removing it')
    if not lib._exists(lib.BOOK_DIR + '/' + book_id + '.prb'):
        raise ValueError('Book not found on the badge')
    lib.remove_download({'id': book_id})
    _generation += 1


def receive(name, length, initial, connection):
    global _generation
    stem, ext = _safe_name(name)
    limit = MAX_UPLOAD if ext == 'prb' else MAX_SIDECAR
    if length <= 0 or length > limit or len(initial) > length:
        raise ValueError('Invalid file size')
    try:
        os.mkdir(lib.BOOK_DIR)
    except OSError:
        pass
    stats = os.statvfs(lib.BOOK_DIR)
    if stats[0] * stats[3] < length + 32768:
        raise ValueError('Not enough free space; remove a download first')
    temporary = lib.BOOK_DIR + '/.pocketreader-upload.tmp'
    digest = hashlib.sha256()
    try:
        with open(temporary, 'wb') as out:
            chunk = initial
            received = 0
            while True:
                if chunk:
                    received += len(chunk)
                    if received > length:
                        raise ValueError('File exceeds declared length')
                    out.write(chunk)
                    digest.update(chunk)
                if received == length:
                    break
                chunk = connection.recv(min(2048, length - received))
                if not chunk:
                    raise ValueError('Upload was interrupted')
        with open(temporary, 'rb') as source:
            if ext == 'prb':
                header_line = source.readline(65537)
                if len(header_line) > 65536:
                    raise ValueError('Book header is too large')
                info = json.loads(header_line)
                if not isinstance(info, dict) or info.get('schema') not in (1, 2, 3):
                    raise ValueError('Unsupported book file')
                stem = lib._safe_id(info.get('id'))
                if not info.get('title') or not info.get('author'):
                    raise ValueError('Book metadata is incomplete')
            else:
                info = json.load(source)
                if not isinstance(info, dict) or info.get('book_id') != stem:
                    raise ValueError('Sidecar book ID does not match filename')
                if ext == 'pri' and (info.get('version') != 1 or not isinstance(info.get('sizes'), dict)):
                    raise ValueError('Unsupported index file')
                if ext == 'prp' and not isinstance(info.get('bookmarks', []), list):
                    raise ValueError('Invalid bookmarks')
        target = lib.BOOK_DIR + '/' + stem + '.' + ext
        if stem == _open_book_id:
            raise ValueError('Close this book on the badge before uploading its files')
        if ext == 'prb' and lib._exists(target):
            raise ValueError('This book is already on the device')
        if ext in ('prp', 'pri') and not lib._exists(lib.BOOK_DIR + '/' + stem + '.prb'):
            raise ValueError('Upload the matching PRB book first')
        if ext == 'prb':
            digest_hex = binascii.hexlify(digest.digest()).decode()
            info = {'id': stem, 'title': info['title'], 'author': info['author'],
                    'year': info.get('year', 0), 'word_count': info.get('word_count', 0),
                    'series': info.get('series', ''), 'series_position': info.get('series_position', ''),
                    'size': length, 'sha256': digest_hex,
                    'package': stem + '.prb', 'local_only': True}
            catalog = lib._read_json(lib.LOCAL_CATALOG_PATH, {})
            books = [entry for entry in catalog.get('books', []) if entry.get('id') != stem]
            books.append(info)
            state = lib.installed_state()
            previous_state = dict(state)
            state[stem] = {'sha256': digest_hex, 'size': length, 'local_only': True,
                           'download_order': lib._next_sequence()}
        previous = target + '.previous'
        had_previous = lib._exists(target)
        if had_previous:
            if lib._exists(previous):
                raise ValueError('Previous file recovery is needed')
            os.rename(target, previous)
        try:
            os.rename(temporary, target)
        except Exception:
            if had_previous:
                os.rename(previous, target)
            raise
        if had_previous:
            os.remove(previous)
        if ext == 'prb':
            try:
                lib._write_json(lib.LOCAL_CATALOG_PATH, {'books': books})
                lib._write_json(lib.CACHE_STATE_PATH, state)
            except Exception:
                try: lib._write_json(lib.LOCAL_CATALOG_PATH, catalog)
                except Exception: pass
                try: lib._write_json(lib.CACHE_STATE_PATH, previous_state)
                except Exception: pass
                try: os.remove(target)
                except OSError: pass
                raise
        _generation += 1
        return stem + '.' + ext
    finally:
        try: os.remove(temporary)
        except OSError: pass
