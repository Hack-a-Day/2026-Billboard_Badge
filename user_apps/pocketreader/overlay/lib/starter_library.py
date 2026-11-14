"""Optional public starter collection for PocketReader XL."""
import asyncio as aio
import binascii
import hashlib
import json
import os
from lib import book_library, wifi_setup

BASE = 'https://prxl.geektoybox.com/library/'
CATALOG_PATH = '/data/pocketreader_starter_catalog.json'
MAX_CATALOG = 32768
MAX_BOOK = 4 * 1024 * 1024
MAX_INDEX = 1024 * 1024


def enabled():
    settings = book_library._read_json(book_library.PREFERENCES_PATH, {})
    return isinstance(settings, dict) and settings.get('starter_library') is True


def set_enabled(value):
    settings = book_library._read_json(book_library.PREFERENCES_PATH, {})
    if not isinstance(settings, dict):
        settings = {}
    settings['starter_library'] = bool(value)
    book_library._write_json(book_library.PREFERENCES_PATH, settings)


def load_catalog():
    if not enabled():
        return []
    catalog = book_library._read_json(CATALOG_PATH, {})
    return catalog.get('books', []) if isinstance(catalog, dict) and catalog.get('schema') == 1 else []


def _valid_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _validate_catalog(data):
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('books'), list) or len(data['books']) > 100:
        raise book_library.LibraryError('Invalid starter catalog')
    seen = set()
    for entry in data['books']:
        if not isinstance(entry, dict):
            raise book_library.LibraryError('Invalid starter book')
        book_id = book_library._safe_id(entry.get('id'))
        if book_id in seen or not isinstance(entry.get('title'), str) or not isinstance(entry.get('author'), str):
            raise book_library.LibraryError('Invalid starter book')
        seen.add(book_id)
        if not _valid_hash(entry.get('sha256')) or not _valid_hash(entry.get('index_sha256')):
            raise book_library.LibraryError('Invalid starter book checksum')
        if type(entry.get('size')) is not int or not 0 < entry['size'] <= MAX_BOOK:
            raise book_library.LibraryError('Invalid starter book size')
        if type(entry.get('index_size')) is not int or not 0 < entry['index_size'] <= MAX_INDEX:
            raise book_library.LibraryError('Invalid starter index size')
    return data


async def _catalog_bytes():
    if not wifi_setup.radio_status()[1]:
        raise book_library.LibraryError('Connect Wi-Fi first')
    reader, writer = await aio.wait_for(
        aio.open_connection('prxl.geektoybox.com', 443, ssl=True), 15)
    try:
        writer.write(b'GET /library/device.json HTTP/1.0\r\nHost: prxl.geektoybox.com\r\nConnection: close\r\n\r\n')
        await aio.wait_for(writer.drain(), 15)
        status = await aio.wait_for(reader.readline(), 15)
        if len(status) > 1024 or len(status.split()) < 2 or status.split()[1] != b'200':
            raise book_library.LibraryError('Starter library HTTP error')
        header_size = 0
        content_length = None
        while True:
            line = await aio.wait_for(reader.readline(), 15)
            header_size += len(line)
            if not line or header_size > 8192:
                raise book_library.LibraryError('Invalid starter library response')
            if line in (b'\r\n', b'\n'):
                break
            name, sep, value = line.partition(b':')
            if not sep:
                raise book_library.LibraryError('Invalid starter library response')
            name, value = name.strip().lower(), value.strip().lower()
            if name == b'content-length':
                content_length = int(value)
                if content_length < 0 or content_length > MAX_CATALOG:
                    raise book_library.LibraryError('Starter catalog is too large')
            if name == b'transfer-encoding' and value != b'identity':
                raise book_library.LibraryError('Unsupported starter library response')
        raw = bytearray()
        while True:
            chunk = await aio.wait_for(reader.read(min(2048, MAX_CATALOG + 1 - len(raw))), 15)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_CATALOG:
                raise book_library.LibraryError('Starter catalog is too large')
            if content_length is not None and len(raw) >= content_length:
                break
        if content_length is not None and len(raw) != content_length:
            raise book_library.LibraryError('Incomplete starter catalog')
        return raw
    finally:
        writer.close()
        await writer.wait_closed()


async def refresh():
    if not enabled():
        return []
    try:
        raw = await _catalog_bytes()
        data = _validate_catalog(json.loads(raw))
        book_library._write_json(CATALOG_PATH, data)
        return data['books']
    except Exception as exc:
        raise book_library.LibraryError('Starter library unavailable: ' + str(exc))


async def _open_download(url, expected_size):
    if not wifi_setup.radio_status()[1]:
        raise book_library.LibraryError('Connect Wi-Fi first')
    if not url.startswith(BASE):
        raise book_library.LibraryError('Invalid starter download URL')
    name = url[len(BASE):]
    if not name or '/' in name or '..' in name or '\r' in name or '\n' in name:
        raise book_library.LibraryError('Invalid starter download name')
    reader, writer = await aio.wait_for(
        aio.open_connection('prxl.geektoybox.com', 443, ssl=True), 15)
    try:
        request = ('GET /library/' + name + ' HTTP/1.0\r\n'
                   'Host: prxl.geektoybox.com\r\nConnection: close\r\n\r\n')
        writer.write(request.encode())
        await aio.wait_for(writer.drain(), 15)
        status = await aio.wait_for(reader.readline(), 15)
        parts = status.split()
        if len(status) > 1024 or len(parts) < 2 or parts[1] != b'200':
            raise book_library.LibraryError('Starter library HTTP error')
        header_size = 0
        while True:
            line = await aio.wait_for(reader.readline(), 15)
            header_size += len(line)
            if not line or header_size > 8192:
                raise book_library.LibraryError('Invalid starter library response')
            if line in (b'\r\n', b'\n'):
                break
            key, sep, value = line.partition(b':')
            if not sep:
                raise book_library.LibraryError('Invalid starter library response')
            key, value = key.strip().lower(), value.strip().lower()
            if key == b'content-length' and int(value) != expected_size:
                raise book_library.LibraryError('Download size differs from catalog')
            if key == b'transfer-encoding' and value != b'identity':
                raise book_library.LibraryError('Unsupported starter library response')
        return reader, writer
    except BaseException:
        writer.close()
        await writer.wait_closed()
        raise


async def _fetch_file(url, temporary, expected_size, expected_hash):
    reader, writer = await _open_download(url, expected_size)
    digest = hashlib.sha256()
    received = 0
    try:
        with open(temporary, 'wb') as output:
            while True:
                chunk = await aio.wait_for(reader.read(min(2048, expected_size + 1 - received)), 15)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size:
                    raise book_library.LibraryError('Download too large')
                digest.update(chunk)
                output.write(chunk)
                await aio.sleep_ms(0)
    finally:
        writer.close()
        await writer.wait_closed()
    if received != expected_size or binascii.hexlify(digest.digest()).decode() != expected_hash:
        raise book_library.LibraryError('Download verification failed')


async def _fetch_starter_file(name, temporary, expected_size, expected_hash):
    error = None
    for base in (BASE,):
        try:
            await _fetch_file(base + name, temporary, expected_size, expected_hash)
            return
        except Exception as exc:
            error = exc
            try:
                os.remove(temporary)
            except OSError:
                pass
    raise book_library.LibraryError('Starter download failed: ' + str(error))


async def download(entry):
    if not enabled():
        raise book_library.LibraryError('Starter library is off')
    _validate_catalog({'schema': 1, 'books': [entry]})
    from lib import book_transfer
    book_id = book_library._safe_id(entry['id'])
    path = book_library.package_path(entry)
    index = book_library.BOOK_DIR + '/' + book_id + '.pri'
    if book_library._exists(path):
        raise book_library.LibraryError('Book already exists; refresh library')
    if book_library._exists(index):
        raise book_library.LibraryError('An index already exists for this book')

    # Resolve the selected title against the live catalog before considering
    # eviction: cached index sizes can change after a library update.
    catalog = _validate_catalog(json.loads(await _catalog_bytes()))
    entry = None
    for item in catalog['books']:
        if item['id'] == book_id:
            entry = item
            break
    if entry is None:
        raise book_library.LibraryError('Book is no longer in the library')
    # Verify both URLs and advertised sizes before removing any existing copy.
    for suffix, size in (('.pri', entry['index_size']), ('.prb', entry['size'])):
        _, writer = await _open_download(BASE + book_id + suffix, size)
        writer.close()
        try:
            await aio.wait_for(writer.wait_closed(), 2)
        except Exception:
            # A server may still be sending the body after the header check.
            pass
    try:
        os.mkdir(book_library.BOOK_DIR)
    except OSError:
        pass
    recoverable = {item['id']: item['sha256'] for item in catalog['books']}
    book_library.ensure_space(entry['size'] + entry['index_size'] + MAX_CATALOG + 32768,
                              book_id, recoverable=recoverable)
    book_library._write_json(CATALOG_PATH, catalog)
    book_tmp, index_tmp = path + '.tmp', index + '.tmp'
    try:
        await _fetch_starter_file(book_id + '.pri', index_tmp, entry['index_size'], entry['index_sha256'])
        with open(index_tmp, 'r') as file:
            sidecar = json.load(file)
        if not isinstance(sidecar, dict) or sidecar.get('version') != 1 or sidecar.get('book_id') != book_id or sidecar.get('sha256') != entry['sha256']:
            raise book_library.LibraryError('Invalid starter index')
        await _fetch_starter_file(book_id + '.prb', book_tmp, entry['size'], entry['sha256'])
        if book_library._exists(path) or book_library._exists(index):
            raise book_library.LibraryError('Book appeared during download')
        os.rename(book_tmp, path)
        try:
            os.rename(index_tmp, index)
            book_transfer.register_installed_book(book_id, entry['sha256'])
        except Exception:
            # Preserve already written files if registration fails; they can be recovered.
            raise
        return path
    finally:
        for temporary in (book_tmp, index_tmp):
            try:
                os.remove(temporary)
            except OSError:
                pass
